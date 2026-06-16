"""
agent.py — RAG core: busca semântica no ChromaDB + chamada OpenRouter.
"""

import os
import logging
import requests
import chromadb
from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

CHROMA_HOST       = os.environ.get("CHROMADB_HOST", "chromadb")
CHROMA_PORT       = int(os.environ.get("CHROMADB_PORT", 8000))
OPENROUTER_KEY    = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL  = os.environ.get("OPENROUTER_MODEL", "nousresearch/hermes-3-llama-3.1-70b")
OPENROUTER_URL    = "https://openrouter.ai/api/v1/chat/completions"

COLLECTIONS = ["reunioes", "projetos", "stakeholders", "analises", "referencias", "inbox"]

# Carregado uma vez na inicialização do container
_model  = None
_chroma = None


def get_model():
    global _model
    if _model is None:
        log.info("Carregando modelo de embeddings...")
        _model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
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

    query_embedding = model.encode([query])[0].tolist()
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
