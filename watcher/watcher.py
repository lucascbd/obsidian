"""
watcher.py — Sincroniza mudanças do CouchDB para o ChromaDB em tempo real.
Usa CouchDB _changes feed (longpoll) para detectar notas novas ou editadas.
"""

import os
import time
import logging
import requests
import chromadb
from fastembed import TextEmbedding

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watcher] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
COUCHDB_URL  = os.environ["COUCHDB_URL"]
COUCHDB_USER = os.environ["COUCHDB_USER"]
COUCHDB_PASS = os.environ["COUCHDB_PASSWORD"]
COUCHDB_DB   = os.environ["COUCHDB_DB"]
CHROMA_HOST  = os.environ.get("CHROMADB_HOST", "chromadb")
CHROMA_PORT  = int(os.environ.get("CHROMADB_PORT", 8000))
AUTH         = (COUCHDB_USER, COUCHDB_PASS)

# Mapeamento de pasta → coleção ChromaDB
FOLDER_MAP = {
    "06-Reunioes":     "reunioes",
    "01-Projetos":     "projetos",
    "04-Stakeholders": "stakeholders",
    "05-Fontes":       "referencias",
    "07-Referencias":  "referencias",
    "02-Mercados":     "analises",
    "03-Marcas":       "analises",
    "00-Inbox":        "inbox",
}

CHUNK_SIZE    = 400   # palavras por chunk
CHUNK_OVERLAP = 40    # overlap entre chunks


# ── Helpers ──────────────────────────────────────────────────────────────────
def get_collection_name(note_id: str) -> str:
    for prefix, col in FOLDER_MAP.items():
        if note_id.startswith(prefix):
            return col
    return "inbox"


def chunk_text(text: str) -> list[str]:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + CHUNK_SIZE]))
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def wait_for_services():
    """Aguarda CouchDB e ChromaDB ficarem prontos."""
    log.info("Aguardando CouchDB...")
    while True:
        try:
            r = requests.get(f"{COUCHDB_URL}/", auth=AUTH, timeout=5)
            if r.status_code == 200:
                log.info("CouchDB OK")
                break
        except Exception:
            pass
        time.sleep(3)

    log.info("Aguardando ChromaDB...")
    while True:
        try:
            r = requests.get(f"http://{CHROMA_HOST}:{CHROMA_PORT}/api/v1/heartbeat", timeout=5)
            if r.status_code == 200:
                log.info("ChromaDB OK")
                break
        except Exception:
            pass
        time.sleep(3)


def ensure_db():
    """Cria o banco CouchDB se não existir."""
    url = f"{COUCHDB_URL}/{COUCHDB_DB}"
    r = requests.get(url, auth=AUTH)
    if r.status_code == 404:
        requests.put(url, auth=AUTH)
        log.info(f"Banco '{COUCHDB_DB}' criado.")


# ── Indexação ────────────────────────────────────────────────────────────────
def index_note(doc: dict, chroma: chromadb.HttpClient, model: TextEmbedding):
    note_id = doc.get("_id", "")

    # Ignora documentos internos do CouchDB e do LiveSync
    if note_id.startswith("_") or note_id.startswith("h:"):
        return

    content = doc.get("content", "") or doc.get("data", "")
    if not content or not content.strip():
        return

    col_name   = get_collection_name(note_id)
    collection = chroma.get_or_create_collection(col_name)

    # Remove versão anterior da nota (pode ter sido editada)
    try:
        collection.delete(where={"note_id": note_id})
    except Exception:
        pass

    chunks     = chunk_text(content)
    embeddings = [e.tolist() for e in model.embed(chunks)]

    collection.add(
        documents=chunks,
        embeddings=embeddings,
        ids=[f"{note_id}__chunk_{i}" for i in range(len(chunks))],
        metadatas=[
            {"note_id": note_id, "chunk": i, "collection": col_name}
            for i in range(len(chunks))
        ],
    )
    log.info(f"Indexado [{col_name}] {note_id} — {len(chunks)} chunk(s)")


def delete_note(note_id: str, chroma: chromadb.HttpClient):
    if note_id.startswith("_") or note_id.startswith("h:"):
        return
    col_name = get_collection_name(note_id)
    try:
        collection = chroma.get_collection(col_name)
        collection.delete(where={"note_id": note_id})
        log.info(f"Removido [{col_name}] {note_id}")
    except Exception:
        pass


# ── Full reindex ─────────────────────────────────────────────────────────────
def full_reindex(chroma: chromadb.HttpClient, model: TextEmbedding):
    log.info("Iniciando reindexação completa...")
    skip, limit = 0, 100
    total = 0
    while True:
        r = requests.get(
            f"{COUCHDB_URL}/{COUCHDB_DB}/_all_docs",
            params={"include_docs": True, "limit": limit, "skip": skip},
            auth=AUTH,
        )
        rows = r.json().get("rows", [])
        if not rows:
            break
        for row in rows:
            doc = row.get("doc", {})
            index_note(doc, chroma, model)
            total += 1
        skip += limit
    log.info(f"Reindexação completa — {total} documento(s) processado(s).")


# ── Changes feed ─────────────────────────────────────────────────────────────
def watch(chroma: chromadb.HttpClient, model: TextEmbedding):
    last_seq = "0"
    log.info("Monitorando mudanças no CouchDB...")

    while True:
        try:
            r = requests.get(
                f"{COUCHDB_URL}/{COUCHDB_DB}/_changes",
                params={
                    "since":        last_seq,
                    "include_docs": True,
                    "feed":         "longpoll",
                    "timeout":      30000,
                },
                auth=AUTH,
                timeout=40,
            )
            data = r.json()

            for change in data.get("results", []):
                doc = change.get("doc", {})
                if change.get("deleted"):
                    delete_note(doc.get("_id", ""), chroma)
                else:
                    index_note(doc, chroma, model)

            last_seq = data.get("last_seq", last_seq)

        except requests.exceptions.Timeout:
            pass  # normal no longpoll
        except Exception as e:
            log.error(f"Erro no watcher: {e} — retentando em 5s")
            time.sleep(5)


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    wait_for_services()
    ensure_db()

    log.info("Carregando modelo de embeddings (paraphrase-multilingual-MiniLM-L12-v2)...")
    model = TextEmbedding("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    chroma = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)

    full_reindex(chroma, model)
    watch(chroma, model)
