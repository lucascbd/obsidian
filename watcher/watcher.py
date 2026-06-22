"""
watcher.py — Multi-vault: assiste TODOS os bancos CouchDB, indexa no ChromaDB.
Uma coleção ChromaDB por vault. Frontmatter como metadata. Grafo de links em _local/.
"""

import os
import re
import time
import logging
import threading
import requests
import chromadb
from fastembed import TextEmbedding

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watcher] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

COUCHDB_URL  = os.environ["COUCHDB_URL"]
COUCHDB_USER = os.environ["COUCHDB_USER"]
COUCHDB_PASS = os.environ["COUCHDB_PASSWORD"]
CHROMA_HOST  = os.environ.get("CHROMADB_HOST", "chromadb")
CHROMA_PORT  = int(os.environ.get("CHROMADB_PORT", 8000))
AUTH         = (COUCHDB_USER, COUCHDB_PASS)

EMBEDDING_MODEL    = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
SYSTEM_DBS         = {"_users", "_replicator", "_global_changes"}
VAULT_POLL_SECS    = 60
CHUNK_SIZE         = 400
CHUNK_OVERLAP      = 50
LINK_GRAPH_DOC     = "_local/link-graph"

_INVALID_COL  = re.compile(r"[^a-zA-Z0-9_-]")
_vault_threads: dict[str, threading.Thread] = {}
_model_lock   = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _col_name(vault: str) -> str:
    """Uma coleção ChromaDB por vault (nome sanitizado)."""
    col = _INVALID_COL.sub("_", vault)[:63]
    return col or "vault"


def _is_settings(note_id: str) -> bool:
    return "00-meta" in note_id.split("/")


def chunk_text(text: str) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + CHUNK_SIZE]))
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def parse_frontmatter(content: str) -> dict:
    """Extrai campos YAML do frontmatter para usar como metadata ChromaDB."""
    meta: dict[str, str] = {}
    if not content.startswith("---"):
        return meta
    end = content.find("\n---", 3)
    if end == -1:
        return meta
    fm = content[3:end]
    for key, pat in [("title", r"^title:\s*(.+)$"),
                     ("date",  r"^date:\s*(.+)$"),
                     ("source", r"^source:\s*(.+)$")]:
        m = re.search(pat, fm, re.MULTILINE)
        if m:
            meta[key] = m.group(1).strip().strip("\"'")
    # tags — lista YAML ou inline
    m = re.search(r"^tags:(.*?)(?=\n\S|\Z)", fm, re.MULTILINE | re.DOTALL)
    if m:
        block = m.group(1)
        tags = re.findall(r"[\-\*]\s*(\S+)", block)
        if not tags:
            tags = [t.strip().strip("\"'") for t in block.strip().strip("[]").split(",") if t.strip()]
        if tags:
            meta["tags"] = ",".join(tags[:10])
    return meta


def extract_wiki_links(content: str) -> list[str]:
    """Extrai note_ids de wiki links [[caminho/nota|alias]] ou [[caminho/nota]]."""
    raw = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content)
    seen, links = set(), []
    for r in raw:
        r = r.strip()
        if not r or r.startswith("http"):
            continue
        if not r.endswith(".md"):
            r += ".md"
        r = r.lower()
        if r not in seen:
            seen.add(r)
            links.append(r)
    return links


def _read_livesync_content(doc: dict, vault: str) -> str:
    """Lê conteúdo real da nota no formato LiveSync (parent + leaf docs)."""
    children = doc.get("children", [])
    if children:
        parts = []
        for leaf_id in children:
            try:
                r = requests.get(
                    f"{COUCHDB_URL}/{vault}/{requests.utils.quote(leaf_id, safe='')}",
                    auth=AUTH, timeout=10,
                )
                if r.status_code == 200:
                    parts.append(r.json().get("data", ""))
            except Exception:
                pass
        return "".join(parts)
    return doc.get("content", "") or doc.get("data", "")


# ── Grafo de links (_local não replica via LiveSync) ──────────────────────────

def _read_link_graph(vault: str) -> dict:
    try:
        r = requests.get(f"{COUCHDB_URL}/{vault}/{LINK_GRAPH_DOC}", auth=AUTH, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"_id": LINK_GRAPH_DOC, "links": {}}


def _write_link_graph(vault: str, doc: dict):
    try:
        requests.put(f"{COUCHDB_URL}/{vault}/{LINK_GRAPH_DOC}", json=doc, auth=AUTH, timeout=10)
    except Exception as e:
        log.warning(f"[{vault}] link-graph write: {e}")


def update_link_graph(vault: str, note_id: str, links: list[str], deleted: bool = False):
    doc = _read_link_graph(vault)
    link_map: dict = doc.get("links", {})
    if deleted or not links:
        link_map.pop(note_id, None)
    else:
        link_map[note_id] = links
    doc["links"] = link_map
    _write_link_graph(vault, doc)


# ── Indexação ─────────────────────────────────────────────────────────────────

def index_note(doc: dict, vault: str, col, model: TextEmbedding):
    note_id = doc.get("_id", "")

    # Filtra internos, leaves, settings, deletados
    if (not note_id or note_id.startswith("_") or note_id.startswith("h:")
            or doc.get("deleted") or _is_settings(note_id)):
        return

    content = _read_livesync_content(doc, vault)
    if not content or not content.strip():
        return

    fm    = parse_frontmatter(content)
    links = extract_wiki_links(content)

    update_link_graph(vault, note_id, links)

    # Remove chunks antigos desta nota (upsert)
    try:
        col.delete(where={"note_id": note_id})
    except Exception:
        pass

    chunks = chunk_text(content)
    if not chunks:
        return

    with _model_lock:
        embeddings = [e.tolist() for e in model.embed(chunks)]

    base_meta = {"note_id": note_id, "vault": vault, **fm}
    col.add(
        documents  = chunks,
        embeddings = embeddings,
        ids        = [f"{vault}__{note_id}__c{i}" for i in range(len(chunks))],
        metadatas  = [{**base_meta, "chunk": i} for i in range(len(chunks))],
    )
    log.info(f"[{vault}] {note_id} — {len(chunks)} chunk(s), {len(links)} link(s)")


def delete_note_from_index(note_id: str, vault: str, chroma: chromadb.HttpClient):
    if not note_id or note_id.startswith("_") or note_id.startswith("h:"):
        return
    try:
        chroma.get_or_create_collection(_col_name(vault)).delete(where={"note_id": note_id})
        log.info(f"[{vault}] Removido {note_id}")
    except Exception:
        pass
    update_link_graph(vault, note_id, [], deleted=True)


# ── Reindexação e watch por vault ─────────────────────────────────────────────

def full_reindex(vault: str, chroma: chromadb.HttpClient, model: TextEmbedding):
    log.info(f"[{vault}] Reindexação completa...")

    # Limpa coleção antiga (dimensões podem ter mudado com novo modelo)
    try:
        chroma.delete_collection(_col_name(vault))
        log.info(f"[{vault}] Coleção anterior removida")
    except Exception:
        pass

    # Cria coleção fresca uma única vez
    col = chroma.get_or_create_collection(_col_name(vault))

    # Reseta grafo de links
    _write_link_graph(vault, {"_id": LINK_GRAPH_DOC, "links": {}})

    skip, limit, total = 0, 100, 0
    while True:
        try:
            r = requests.get(
                f"{COUCHDB_URL}/{vault}/_all_docs",
                params={"include_docs": True, "limit": limit, "skip": skip},
                auth=AUTH, timeout=30,
            )
            rows = r.json().get("rows", [])
        except Exception as e:
            log.error(f"[{vault}] _all_docs erro: {e}")
            break
        if not rows:
            break
        for row in rows:
            index_note(row.get("doc", {}), vault, col, model)
            total += 1
        skip += limit
    log.info(f"[{vault}] Reindexação: {total} docs processados")


def watch_vault(vault: str, chroma: chromadb.HttpClient, model: TextEmbedding):
    log.info(f"[{vault}] Watch iniciado")
    col      = chroma.get_or_create_collection(_col_name(vault))
    last_seq = "0"
    while True:
        try:
            r = requests.get(
                f"{COUCHDB_URL}/{vault}/_changes",
                params={
                    "since":        last_seq,
                    "include_docs": True,
                    "feed":         "longpoll",
                    "timeout":      30000,
                },
                auth=AUTH, timeout=40,
            )
            data = r.json()
            for change in data.get("results", []):
                doc = change.get("doc", {})
                if change.get("deleted"):
                    delete_note_from_index(doc.get("_id", ""), vault, chroma)
                else:
                    index_note(doc, vault, col, model)
            last_seq = data.get("last_seq", last_seq)
        except requests.exceptions.Timeout:
            pass  # normal no longpoll
        except Exception as e:
            log.error(f"[{vault}] Watch erro: {e} — retentando em 5s")
            time.sleep(5)


# ── Descoberta dinâmica de vaults ─────────────────────────────────────────────

def get_active_vaults() -> list[str]:
    try:
        r = requests.get(f"{COUCHDB_URL}/_all_dbs", auth=AUTH, timeout=10)
        return [v for v in r.json() if v not in SYSTEM_DBS]
    except Exception as e:
        log.warning(f"_all_dbs erro: {e}")
        return []


def ensure_vault_watched(vault: str, chroma: chromadb.HttpClient, model: TextEmbedding):
    t = _vault_threads.get(vault)
    if t and t.is_alive():
        return

    # Verifica se a coleção já existe (pode ter sido criada pelo agent/reindex externo)
    col_nm = _col_name(vault)
    col_exists = False
    try:
        chroma.get_collection(col_nm)
        col_exists = True
    except Exception:
        pass

    if col_exists:
        log.info(f"[{vault}] Coleção já existe — pulando full_reindex, iniciando watch")
    else:
        log.info(f"[{vault}] Novo vault — iniciando reindexação e watch")
        full_reindex(vault, chroma, model)

    t = threading.Thread(target=watch_vault, args=(vault, chroma, model), daemon=True, name=f"watch-{vault}")
    t.start()
    _vault_threads[vault] = t


# ── Startup ───────────────────────────────────────────────────────────────────

def wait_for_services():
    log.info("Aguardando CouchDB...")
    while True:
        try:
            if requests.get(f"{COUCHDB_URL}/", auth=AUTH, timeout=5).status_code == 200:
                log.info("CouchDB OK")
                break
        except Exception:
            pass
        time.sleep(3)

    log.info("Aguardando ChromaDB...")
    while True:
        try:
            for path in ("/api/v2/heartbeat", "/api/v1/heartbeat"):
                try:
                    if requests.get(f"http://{CHROMA_HOST}:{CHROMA_PORT}{path}", timeout=5).status_code == 200:
                        log.info("ChromaDB OK")
                        return
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(3)


if __name__ == "__main__":
    wait_for_services()

    log.info(f"Carregando modelo: {EMBEDDING_MODEL}")
    model  = TextEmbedding(EMBEDDING_MODEL)
    chroma = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)

    log.info("Modo multi-vault — polling a cada 60s para novos vaults")
    while True:
        for vault in get_active_vaults():
            ensure_vault_watched(vault, chroma, model)
        time.sleep(VAULT_POLL_SECS)
