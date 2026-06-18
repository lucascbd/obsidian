"""
agent.py — Dois modos:
  1. RAG (ask): busca semântica + resposta contextual (somente leitura)
  2. Task (execute): tool use em loop — lê, move, edita, cria notas até terminar
"""

import os
import json
import logging
import requests
import chromadb
from sentence_transformers import SentenceTransformer
from vault_tools import TOOL_DEFINITIONS, TOOL_FUNCTIONS

log = logging.getLogger(__name__)

CHROMA_HOST      = os.environ.get("CHROMADB_HOST", "chromadb")
CHROMA_PORT      = int(os.environ.get("CHROMADB_PORT", 8000))
OPENROUTER_KEY   = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "nousresearch/hermes-3-llama-3.1-70b")
OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"
MAX_TOOL_ROUNDS  = 50  # limite de segurança para evitar loop infinito

COLLECTIONS = ["reunioes", "projetos", "stakeholders", "analises", "referencias", "inbox"]

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
    cols   = collections or COLLECTIONS
    model  = get_model()
    chroma = get_chroma()
    query_embedding = model.encode([query])[0].tolist()
    results = []

    for col_name in cols:
        try:
            col = chroma.get_collection(col_name)
            r   = col.query(query_embeddings=[query_embedding], n_results=n_results)
            for doc, meta, distance in zip(r["documents"][0], r["metadatas"][0], r["distances"][0]):
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
def call_llm(messages: list[dict], tools: list[dict] | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://obsidian-agent.local",
        "X-Title":       "Obsidian MI Agent",
    }
    payload: dict = {
        "model":       OPENROUTER_MODEL,
        "messages":    messages,
        "temperature": 0.2,
    }
    if tools:
        payload["tools"]       = tools
        payload["tool_choice"] = "auto"

    r = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


# ── Prompts ───────────────────────────────────────────────────────────────────
SYSTEM_RAG = """Você é um assistente de inteligência de mercado e gestão de conhecimento.
Analisa notas de trabalho do Obsidian e responde com base apenas no contexto fornecido.
Seja direto, acionável e sempre cite de qual nota veio a informação (use o formato [pasta/nota]).
Responda sempre em português brasileiro."""

SYSTEM_TASK = """Você é um agente executor que gerencia um vault do Obsidian.
Você tem acesso a ferramentas para ler, criar, editar, mover e apagar notas.

Regras:
- Execute a tarefa completamente até o fim, sem parar para pedir confirmação.
- Sempre leia (list_notes / read_note) antes de editar ou mover.
- Ao reestruturar, processe TODAS as notas do escopo — não faça apenas algumas.
- Ao mover notas, atualize os wikilinks das outras notas que apontavam para o caminho antigo.
- Responda sempre em português brasileiro.
- Quando terminar TUDO, escreva um resumo final do que foi feito."""


# ── Modo 1: RAG (perguntas) ───────────────────────────────────────────────────
def ask(question: str, collections: list[str] | None = None) -> dict:
    chunks = search_vault(question, collections)
    if not chunks:
        return {"answer": "Nenhuma nota relevante encontrada.", "sources": []}
    context = build_context(chunks)
    msg = call_llm([
        {"role": "system", "content": SYSTEM_RAG},
        {"role": "user",   "content": f"Contexto do vault:\n\n{context}\n\n---\n\nPergunta: {question}"},
    ])
    return {"answer": msg.get("content", ""), "sources": list({c["note_id"] for c in chunks})}


# ── Modo 2: Task (execução com tool use) ─────────────────────────────────────
def execute_task(instruction: str) -> dict:
    """
    Recebe uma instrução em linguagem natural e executa no vault usando tool use.
    O LLM decide quais ferramentas chamar, em que ordem, até completar a tarefa.
    """
    messages = [
        {"role": "system", "content": SYSTEM_TASK},
        {"role": "user",   "content": instruction},
    ]
    actions_log = []
    rounds = 0

    while rounds < MAX_TOOL_ROUNDS:
        rounds += 1
        response = call_llm(messages, tools=TOOL_DEFINITIONS)

        # Adiciona resposta do assistente ao histórico
        messages.append({"role": "assistant", **{k: v for k, v in response.items() if k != "role"}})

        tool_calls = response.get("tool_calls")

        # Sem mais tool calls → agente terminou
        if not tool_calls:
            final_answer = response.get("content", "Tarefa concluída.")
            log.info(f"execute_task: concluído em {rounds} rodada(s), {len(actions_log)} ação(ões)")
            return {
                "answer":  final_answer,
                "actions": actions_log,
                "rounds":  rounds,
                "sources": [],
            }

        # Executa cada tool call e devolve o resultado para o LLM
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception:
                args = {}

            log.info(f"Tool call: {fn_name}({args})")

            fn = TOOL_FUNCTIONS.get(fn_name)
            if fn:
                try:
                    result = fn(**args)
                except Exception as e:
                    result = {"error": str(e)}
            else:
                result = {"error": f"Ferramenta desconhecida: {fn_name}"}

            actions_log.append({"tool": fn_name, "args": args, "result": result})

            messages.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      json.dumps(result, ensure_ascii=False),
            })

    return {
        "answer":  f"Limite de {MAX_TOOL_ROUNDS} rodadas atingido. Verifique o log de ações.",
        "actions": actions_log,
        "rounds":  rounds,
        "sources": [],
    }


# ── Ações pré-definidas ───────────────────────────────────────────────────────
def weekly_summary() -> dict:
    chunks  = search_vault("projetos em andamento riscos próximos passos entregas", ["projetos", "reunioes"])
    context = build_context(chunks)
    msg = call_llm([
        {"role": "system", "content": SYSTEM_RAG},
        {"role": "user", "content":
            f"Contexto:\n\n{context}\n\n---\n\n"
            "Gere um resumo executivo semanal com:\n"
            "1. Projetos em andamento e status\n"
            "2. Riscos e bloqueios\n"
            "3. Próximos passos prioritários\n"
            "4. Stakeholders que precisam de atenção"},
    ])
    return {"answer": msg.get("content", ""), "sources": list({c["note_id"] for c in chunks})}


def market_insights() -> dict:
    chunks  = search_vault("insights tendências mercado oportunidades concorrência", ["analises", "referencias", "reunioes"])
    context = build_context(chunks)
    msg = call_llm([
        {"role": "system", "content": SYSTEM_RAG},
        {"role": "user", "content":
            f"Contexto:\n\n{context}\n\n---\n\n"
            "Identifique:\n"
            "1. Principais tendências de mercado\n"
            "2. Oportunidades identificadas\n"
            "3. Riscos competitivos\n"
            "4. Gaps de conhecimento"},
    ])
    return {"answer": msg.get("content", ""), "sources": list({c["note_id"] for c in chunks})}


def summarize_meeting(note_id: str) -> dict:
    chroma = get_chroma()
    model  = get_model()
    try:
        col    = chroma.get_collection("reunioes")
        result = col.get(where={"note_id": note_id}, include=["documents"])
        content = " ".join(result["documents"])
    except Exception:
        return {"answer": f"Reunião '{note_id}' não encontrada.", "sources": []}

    msg = call_llm([
        {"role": "system", "content": SYSTEM_RAG},
        {"role": "user", "content":
            f"Notas da reunião:\n\n{content}\n\n---\n\n"
            "Gere:\n"
            "1. Resumo executivo (3-5 pontos)\n"
            "2. Decisões tomadas\n"
            "3. Action items com responsável e prazo\n"
            "4. Pontos de follow-up"},
    ])
    return {"answer": msg.get("content", ""), "sources": [note_id]}
