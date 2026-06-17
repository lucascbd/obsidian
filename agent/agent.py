"""
agent.py — RAG core: busca semântica no ChromaDB + chamada OpenRouter.
"""

import os
import re
import logging
import requests
import chromadb
from fastembed import TextEmbedding

log = logging.getLogger(__name__)

CHROMA_HOST       = os.environ.get("CHROMADB_HOST", "chromadb")
CHROMA_PORT       = int(os.environ.get("CHROMADB_PORT", 8000))
OPENROUTER_KEY    = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL  = os.environ.get("OPENROUTER_MODEL", "nousresearch/hermes-3-llama-3.1-70b")
OPENROUTER_URL    = "https://openrouter.ai/api/v1/chat/completions"

COUCHDB_URL  = os.environ.get("COUCHDB_URL", "http://couchdb:5984")
COUCHDB_USER = os.environ.get("COUCHDB_USER", "")
COUCHDB_PASS = os.environ.get("COUCHDB_PASSWORD", "")
COUCHDB_DB   = os.environ.get("COUCHDB_DB", "obsidian-vault")
COUCHDB_AUTH = (COUCHDB_USER, COUCHDB_PASS)

COLLECTIONS = ["reunioes", "projetos", "stakeholders", "analises", "referencias", "inbox"]

_model  = None
_chroma = None


def get_model():
    global _model
    if _model is None:
        log.info("Carregando modelo de embeddings...")
        _model = TextEmbedding("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    return _model


def get_chroma():
    global _chroma
    if _chroma is None:
        _chroma = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    return _chroma


# ── CouchDB helpers ───────────────────────────────────────────────────────────
def _couch_get(path: str) -> dict:
    r = requests.get(f"{COUCHDB_URL}/{COUCHDB_DB}/{path}", auth=COUCHDB_AUTH, timeout=10)
    r.raise_for_status()
    return r.json()


def _couch_put(path: str, doc: dict) -> dict:
    r = requests.put(f"{COUCHDB_URL}/{COUCHDB_DB}/{path}", json=doc, auth=COUCHDB_AUTH, timeout=10)
    r.raise_for_status()
    return r.json()


def _read_note(doc: dict) -> str:
    """Concatena o conteúdo de todos os leaves de uma nota."""
    parts = []
    for leaf_id in doc.get("children", []):
        try:
            leaf = _couch_get(requests.utils.quote(leaf_id, safe=""))
            parts.append(leaf.get("data", ""))
        except Exception:
            pass
    return "".join(parts)


def _write_note(doc: dict, new_content: str) -> bool:
    """Distribui o novo conteúdo pelos leaves existentes e salva."""
    children = doc.get("children", [])
    if not children:
        return False

    # Busca todos os leaves com seus _rev
    leaves = []
    for leaf_id in children:
        try:
            leaf = _couch_get(requests.utils.quote(leaf_id, safe=""))
            leaves.append(leaf)
        except Exception:
            return False

    # Divide o conteúdo proporcionalmente ao tamanho original de cada leaf
    total_original = sum(len(l.get("data", "")) for l in leaves)
    if total_original == 0:
        return False

    written = 0
    for i, leaf in enumerate(leaves):
        original_size = len(leaf.get("data", ""))
        if i == len(leaves) - 1:
            chunk = new_content[written:]
        else:
            size = max(1, round(len(new_content) * original_size / total_original))
            chunk = new_content[written:written + size]
            written += size

        leaf["data"] = chunk
        try:
            _couch_put(requests.utils.quote(leaf["_id"], safe=""), leaf)
        except Exception as e:
            log.error(f"Erro ao salvar leaf {leaf['_id']}: {e}")
            return False

    return True


def _all_notes() -> list[dict]:
    """Retorna todos os documentos ativos (não deletados) com seus conteúdos."""
    r = requests.get(
        f"{COUCHDB_URL}/{COUCHDB_DB}/_all_docs",
        params={"include_docs": True},
        auth=COUCHDB_AUTH,
        timeout=30,
    )
    r.raise_for_status()
    notes = []
    for row in r.json().get("rows", []):
        doc = row.get("doc", {})
        if (doc.get("deleted") or
                doc.get("type") != "plain" or
                doc["_id"].startswith("_") or
                doc["_id"].startswith("h:")):
            continue
        content = _read_note(doc)
        if content.strip():
            notes.append({"doc": doc, "content": content})
    return notes


# ── RAG ──────────────────────────────────────────────────────────────────────
def search_vault(query: str, collections: list[str] | None = None, n_results: int = 6) -> list[dict]:
    cols   = collections or COLLECTIONS
    model  = get_model()
    chroma = get_chroma()

    query_embedding = list(model.embed([query]))[0].tolist()
    results = []

    for col_name in cols:
        try:
            col = chroma.get_collection(col_name)
            r   = col.query(query_embeddings=[query_embedding], n_results=n_results)
            for doc, meta, distance in zip(
                r["documents"][0], r["metadatas"][0], r["distances"][0]
            ):
                results.append({
                    "collection": col_name,
                    "note_id":    meta["note_id"],
                    "content":    doc,
                    "score":      round(1 - distance, 3),
                })
        except Exception:
            pass

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:n_results * 2]


def build_context(chunks: list[dict]) -> str:
    context = ""
    for c in chunks:
        context += f"[{c['collection']} / {c['note_id']}] (relevância: {c['score']})\n"
        context += f"{c['content']}\n\n"
    return context.strip()


# ── OpenRouter ───────────────────────────────────────────────────────────────
def call_llm(system: str, user: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://obsidian-agent.local",
        "X-Title":       "Obsidian MI Agent",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "temperature": 0.3,
    }
    r = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# ── Vault review ─────────────────────────────────────────────────────────────
def _fix_wiki_links(content: str) -> str:
    """[[pasta/Nome]] → [[pasta/Nome|Nome]] quando não tem alias."""
    def replacer(m):
        inner = m.group(1)
        if "|" in inner:
            return m.group(0)  # já tem alias
        # pega o último segmento do path sem extensão
        name = inner.split("/")[-1]
        name = re.sub(r"\.md$", "", name)
        return f"[[{inner}|{name}]]"
    return re.sub(r"\[\[([^\]]+)\]\]", replacer, content)


def vault_review() -> dict:
    """
    1. Corrige aliases de wiki links em todas as notas
    2. Pede ao LLM para sugerir correlações entre notas e insere links
    """
    notes = _all_notes()
    if not notes:
        return {"answer": "Nenhuma nota encontrada no vault.", "sources": [], "modified": 0}

    modified = []
    note_index = {n["doc"]["_id"]: n["content"] for n in notes}
    note_ids   = list(note_index.keys())

    for note in notes:
        doc         = note["doc"]
        original    = note["content"]
        updated     = _fix_wiki_links(original)

        # Pede ao LLM para identificar correlações com outras notas
        other_ids = [nid for nid in note_ids if nid != doc["_id"]]
        if other_ids:
            prompt = (
                f"Você está revisando a nota: {doc['_id']}\n\n"
                f"Conteúdo:\n{original[:3000]}\n\n"
                f"Notas existentes no vault:\n" + "\n".join(f"- {nid}" for nid in other_ids[:80]) +
                "\n\nTarefa: Se o conteúdo desta nota menciona implicitamente alguma das outras notas "
                "listadas, adicione um wiki link Obsidian no local mais adequado do texto. "
                "Use o formato [[caminho/da/nota|Nome Visível]]. "
                "Retorne APENAS o conteúdo completo da nota com as modificações aplicadas, "
                "sem explicações, sem markdown extra, sem blocos de código. "
                "Se não houver correlações relevantes, retorne o conteúdo exatamente como está."
            )
            try:
                llm_result = call_llm(
                    "Você é um assistente especialista em gestão de conhecimento em Obsidian. "
                    "Sua tarefa é enriquecer notas com links internos pertinentes. "
                    "Seja conservador — só adicione links quando a correlação for clara e útil.",
                    prompt,
                )
                # Usa resultado do LLM como base, depois aplica fix de aliases novamente
                updated = _fix_wiki_links(llm_result.strip())
            except Exception as e:
                log.error(f"LLM falhou para {doc['_id']}: {e}")

        if updated != original:
            success = _write_note(doc, updated)
            if success:
                modified.append(doc["_id"])
                log.info(f"Revisado: {doc['_id']}")

    summary = (
        f"Revisão concluída. {len(modified)} nota(s) modificada(s).\n\n"
        + ("\n".join(f"- {m}" for m in modified) if modified else "Nenhuma alteração necessária.")
    )
    return {"answer": summary, "sources": modified, "modified": len(modified)}



def get_model():
    global _model
    if _model is None:
        log.info("Carregando modelo de embeddings...")
        _model = TextEmbedding("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    return _model


def get_chroma():
    global _chroma
    if _chroma is None:
        _chroma = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    return _chroma


# ── RAG ──────────────────────────────────────────────────────────────────────
def search_vault(query: str, collections: list[str] | None = None, n_results: int = 6) -> list[dict]:
    """Busca semântica nos embeddings do vault."""
    cols   = collections or COLLECTIONS
    model  = get_model()
    chroma = get_chroma()

    query_embedding = list(model.embed([query]))[0].tolist()
    results = []

    for col_name in cols:
        try:
            col = chroma.get_collection(col_name)
            r   = col.query(query_embeddings=[query_embedding], n_results=n_results)
            for doc, meta, distance in zip(
                r["documents"][0], r["metadatas"][0], r["distances"][0]
            ):
                results.append({
                    "collection": col_name,
                    "note_id":    meta["note_id"],
                    "content":    doc,
                    "score":      round(1 - distance, 3),  # similaridade 0-1
                })
        except Exception:
            pass  # coleção ainda não existe

    # Ordena por relevância
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:n_results * 2]  # retorna os melhores entre todas as coleções


def build_context(chunks: list[dict]) -> str:
    context = ""
    for c in chunks:
        context += f"[{c['collection']} / {c['note_id']}] (relevância: {c['score']})\n"
        context += f"{c['content']}\n\n"
    return context.strip()


# ── OpenRouter ───────────────────────────────────────────────────────────────
def call_llm(system: str, user: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://obsidian-agent.local",
        "X-Title":       "Obsidian MI Agent",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "temperature": 0.3,
    }
    r = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# ── Ações do agente ──────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Você é um assistente de inteligência de mercado e gestão de conhecimento.
Analisa notas de trabalho do Obsidian e responde com base apenas no contexto fornecido.
Seja direto, acionável e sempre cite de qual nota veio a informação (use o formato [pasta/nota]).
Responda sempre em português brasileiro."""


def ask(question: str, collections: list[str] | None = None) -> dict:
    """Pergunta livre com RAG."""
    chunks = search_vault(question, collections)
    if not chunks:
        return {
            "answer":  "Nenhuma nota relevante encontrada para essa pergunta.",
            "sources": [],
        }
    context = build_context(chunks)
    answer  = call_llm(
        SYSTEM_PROMPT,
        f"Contexto do vault:\n\n{context}\n\n---\n\nPergunta: {question}",
    )
    sources = list({c["note_id"] for c in chunks})
    return {"answer": answer, "sources": sources}


def weekly_summary() -> dict:
    """Gera resumo executivo semanal dos projetos."""
    chunks  = search_vault("projetos em andamento riscos próximos passos entregas", ["projetos", "reunioes"])
    context = build_context(chunks)
    answer  = call_llm(
        SYSTEM_PROMPT,
        f"Contexto do vault:\n\n{context}\n\n---\n\n"
        "Gere um resumo executivo semanal com:\n"
        "1. Projetos em andamento e seu status\n"
        "2. Riscos e bloqueios identificados\n"
        "3. Próximos passos prioritários\n"
        "4. Stakeholders que precisam de atenção",
    )
    sources = list({c["note_id"] for c in chunks})
    return {"answer": answer, "sources": sources}


def market_insights() -> dict:
    """Extrai insights de mercado das análises e reuniões."""
    chunks  = search_vault("insights tendências mercado oportunidades concorrência share", ["analises", "referencias", "reunioes"])
    context = build_context(chunks)
    answer  = call_llm(
        SYSTEM_PROMPT,
        f"Contexto do vault:\n\n{context}\n\n---\n\n"
        "Com base nas análises e estudos registrados, identifique:\n"
        "1. Principais tendências de mercado observadas\n"
        "2. Oportunidades identificadas\n"
        "3. Riscos competitivos\n"
        "4. Gaps de conhecimento (o que ainda precisamos investigar)",
    )
    sources = list({c["note_id"] for c in chunks})
    return {"answer": answer, "sources": sources}


def summarize_meeting(note_id: str) -> dict:
    """Sumariza uma reunião específica e extrai action items."""
    chroma = get_chroma()
    model  = get_model()
    try:
        col    = chroma.get_collection("reunioes")
        result = col.get(where={"note_id": note_id}, include=["documents"])
        content = " ".join(result["documents"])
    except Exception:
        return {"answer": f"Reunião '{note_id}' não encontrada.", "sources": []}

    answer = call_llm(
        SYSTEM_PROMPT,
        f"Transcrição/notas da reunião:\n\n{content}\n\n---\n\n"
        "Gere:\n"
        "1. Resumo executivo (3-5 pontos principais)\n"
        "2. Decisões tomadas\n"
        "3. Action items com responsável e prazo (se mencionados)\n"
        "4. Pontos que precisam de follow-up",
    )
    return {"answer": answer, "sources": [note_id]}
