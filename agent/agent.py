"""
agent.py — Agente com tool calling: lê e escreve no vault via CouchDB/ChromaDB.
"""

import os
import re
import json
import logging
import requests
import chromadb
from fastembed import TextEmbedding

log = logging.getLogger(__name__)

CHROMA_HOST      = os.environ.get("CHROMADB_HOST", "chromadb")
CHROMA_PORT      = int(os.environ.get("CHROMADB_PORT", 8000))
OPENROUTER_KEY   = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "nousresearch/hermes-3-llama-3.1-70b")
OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"

COUCHDB_URL  = os.environ.get("COUCHDB_URL", "http://couchdb:5984")
COUCHDB_USER = os.environ.get("COUCHDB_USER", "")
COUCHDB_PASS = os.environ.get("COUCHDB_PASSWORD", "")
COUCHDB_DB   = os.environ.get("COUCHDB_DB", "obsidian-vault")
COUCHDB_AUTH = (COUCHDB_USER, COUCHDB_PASS)

COLLECTIONS = ["reunioes", "projetos", "stakeholders", "analises", "referencias", "inbox"]

_embed_model = None
_chroma      = None


def get_embed_model():
    global _embed_model
    if _embed_model is None:
        log.info("Carregando modelo de embeddings...")
        _embed_model = TextEmbedding("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    return _embed_model


def get_chroma():
    global _chroma
    if _chroma is None:
        _chroma = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    return _chroma


# ── CouchDB ───────────────────────────────────────────────────────────────────
def _couch(method: str, path: str, **kwargs):
    url = f"{COUCHDB_URL}/{COUCHDB_DB}/{requests.utils.quote(path, safe='')}"
    r = getattr(requests, method)(url, auth=COUCHDB_AUTH, timeout=15, **kwargs)
    r.raise_for_status()
    return r.json()


def _read_note_content(doc: dict) -> str:
    parts = []
    for leaf_id in doc.get("children", []):
        try:
            leaf = _couch("get", leaf_id)
            parts.append(leaf.get("data", ""))
        except Exception:
            pass
    return "".join(parts)


def _write_note_content(doc: dict, new_content: str) -> bool:
    children = doc.get("children", [])
    if not children:
        return False
    leaves = []
    for leaf_id in children:
        try:
            leaves.append(_couch("get", leaf_id))
        except Exception:
            return False

    total = sum(len(l.get("data", "")) for l in leaves) or 1
    written = 0
    for i, leaf in enumerate(leaves):
        if i == len(leaves) - 1:
            chunk = new_content[written:]
        else:
            size = max(1, round(len(new_content) * len(leaf.get("data", "")) / total))
            chunk = new_content[written:written + size]
            written += size
        leaf["data"] = chunk
        _couch("put", leaf["_id"], json=leaf)

    # Atualiza size (em bytes, como o LiveSync espera) e mtime no documento pai
    import time
    doc["size"] = len(new_content.encode("utf-8"))
    doc["mtime"] = int(time.time() * 1000)
    _couch("put", doc["_id"], json=doc)
    return True


# ── Tools disponíveis para o agente ──────────────────────────────────────────
def repair_vault() -> dict:
    """Corrige size mismatch em documentos do CouchDB."""
    r = requests.get(
        f"{COUCHDB_URL}/{COUCHDB_DB}/_all_docs",
        params={"include_docs": True},
        auth=COUCHDB_AUTH, timeout=30,
    )
    r.raise_for_status()
    fixed = []
    for row in r.json().get("rows", []):
        doc = row.get("doc", {})
        if doc.get("deleted") or doc.get("type") != "plain" or doc["_id"].startswith("_") or doc["_id"].startswith("h:"):
            continue
        content = _read_note_content(doc)
        real_size = len(content.encode("utf-8"))
        if doc.get("size", -1) != real_size:
            import time
            doc["size"] = real_size
            doc["mtime"] = int(time.time() * 1000)
            try:
                _couch("put", doc["_id"], json=doc)
                fixed.append(f"{doc['_id']} ({doc.get('size', '?')} → {real_size})")
                log.info(f"Reparado: {doc['_id']}")
            except Exception as e:
                log.error(f"Erro ao reparar {doc['_id']}: {e}")
    return {
        "answer": f"{len(fixed)} documento(s) reparado(s).\n" + "\n".join(fixed) if fixed else "Nenhum documento com size incorreto encontrado.",
        "sources": fixed,
    }

def tool_search_vault(query: str, collections: list | None = None) -> str:
    """Busca semântica no vault via ChromaDB."""
    cols  = collections or COLLECTIONS
    model = get_embed_model()
    chroma = get_chroma()
    embedding = list(model.embed([query]))[0].tolist()
    results = []
    for col_name in cols:
        try:
            col = chroma.get_collection(col_name)
            r   = col.query(query_embeddings=[embedding], n_results=5)
            for doc, meta, dist in zip(r["documents"][0], r["metadatas"][0], r["distances"][0]):
                results.append({
                    "note_id": meta["note_id"],
                    "score":   round(1 - dist, 3),
                    "excerpt": doc[:400],
                })
        except Exception:
            pass
    results.sort(key=lambda x: x["score"], reverse=True)
    if not results:
        return "Nenhuma nota encontrada."
    return json.dumps(results[:8], ensure_ascii=False)


def tool_read_note(note_id: str) -> str:
    """Lê o conteúdo completo de uma nota do CouchDB."""
    try:
        doc = _couch("get", note_id)
        content = _read_note_content(doc)
        return content if content else "(nota vazia)"
    except Exception as e:
        return f"Erro ao ler nota '{note_id}': {e}"


def tool_edit_note(note_id: str, content: str) -> str:
    """Edita o conteúdo de uma nota existente no CouchDB."""
    try:
        doc = _couch("get", note_id)
        if doc.get("deleted"):
            return f"Nota '{note_id}' está deletada."
        ok = _write_note_content(doc, content)
        return f"Nota '{note_id}' atualizada com sucesso." if ok else "Falha ao salvar."
    except Exception as e:
        return f"Erro ao editar nota '{note_id}': {e}"


def tool_list_notes() -> str:
    """Lista todas as notas ativas do vault."""
    try:
        r = requests.get(
            f"{COUCHDB_URL}/{COUCHDB_DB}/_all_docs",
            params={"include_docs": False},
            auth=COUCHDB_AUTH, timeout=15,
        )
        r.raise_for_status()
        ids = [
            row["id"] for row in r.json().get("rows", [])
            if not row["id"].startswith("_") and not row["id"].startswith("h:")
        ]
        return json.dumps(ids, ensure_ascii=False)
    except Exception as e:
        return f"Erro ao listar notas: {e}"


def tool_create_note(path: str, content: str) -> str:
    """Cria uma nova nota no vault. path ex: '00-inbox/minha-nota.md'"""
    import hashlib, time
    note_id = path.lower()
    leaf_id = "h:" + hashlib.md5(f"{path}{time.time()}".encode()).hexdigest()[:13]
    leaf = {"_id": leaf_id, "data": content, "type": "leaf"}
    doc  = {
        "_id":      note_id,
        "children": [leaf_id],
        "path":     path,
        "ctime":    int(time.time() * 1000),
        "mtime":    int(time.time() * 1000),
        "size":     len(content.encode("utf-8")),
        "type":     "plain",
        "eden":     {},
    }
    try:
        _couch("put", leaf_id, json=leaf)
        _couch("put", note_id, json=doc)
        return f"Nota '{path}' criada com sucesso."
    except Exception as e:
        return f"Erro ao criar nota: {e}"


# ── Tool calling loop ─────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_vault",
            "description": "Busca semântica nas notas do vault. Use antes de ler notas específicas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":       {"type": "string", "description": "Texto para buscar"},
                    "collections": {"type": "array", "items": {"type": "string"},
                                    "description": "Filtrar por coleções: reunioes, projetos, stakeholders, analises, referencias, inbox"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": "Lê o conteúdo completo de uma nota pelo seu ID (caminho).",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "ID da nota, ex: '06-reunioes/reuniao-x.md'"},
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "Lista todas as notas ativas do vault.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_note",
            "description": "Edita o conteúdo de uma nota existente. Sempre leia a nota antes de editar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "ID da nota a editar"},
                    "content": {"type": "string", "description": "Novo conteúdo completo da nota em markdown"},
                },
                "required": ["note_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": "Cria uma nova nota no vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "Caminho da nota, ex: '00-inbox/nova-nota.md'"},
                    "content": {"type": "string", "description": "Conteúdo da nota em markdown"},
                },
                "required": ["path", "content"],
            },
        },
    },
]

TOOL_FNS = {
    "search_vault": lambda args: tool_search_vault(**args),
    "read_note":    lambda args: tool_read_note(**args),
    "list_notes":   lambda args: tool_list_notes(),
    "edit_note":    lambda args: tool_edit_note(**args),
    "create_note":  lambda args: tool_create_note(**args),
}

SYSTEM_PROMPT = """Você é um agente de gestão de conhecimento integrado ao Obsidian via CouchDB.
Você pode LER e ESCREVER notas diretamente no vault do usuário.

Ferramentas disponíveis (responda APENAS com JSON quando quiser usar uma):

{"tool": "search_vault", "args": {"query": "texto para buscar", "collections": ["opcional"]}}
{"tool": "read_note", "args": {"note_id": "caminho/da/nota.md"}}
{"tool": "list_notes", "args": {}}
{"tool": "edit_note", "args": {"note_id": "caminho/da/nota.md", "content": "conteúdo completo"}}
{"tool": "create_note", "args": {"path": "pasta/nome.md", "content": "conteúdo"}}
{"tool": "done", "args": {"answer": "resposta final para o usuário"}}

Regras:
- Responda SOMENTE com um JSON por vez (sem texto antes ou depois)
- Sempre use search_vault ou list_notes antes de agir
- Leia a nota com read_note antes de editar
- Wiki links devem ter alias: [[caminho/nota|Nome Visível]]
- Preserve frontmatter ao editar notas
- Termine sempre com {"tool": "done", "args": {"answer": "..."}} relatando o que foi feito
- Responda em português brasileiro"""


def _call_llm_raw(messages: list) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://obsidian-agent.local",
        "X-Title":       "Obsidian MI Agent",
    }
    payload = {
        "model":       OPENROUTER_MODEL,
        "messages":    messages,
        "temperature": 0.2,
    }
    r = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=120)
    if not r.ok:
        log.error(f"OpenRouter error {r.status_code}: {r.text}")
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def ask(question: str, collections: list | None = None) -> dict:
    """Loop de tool calling via prompt até o agente chamar 'done'."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": question},
    ]
    sources = []

    for _ in range(20):
        raw = _call_llm_raw(messages)
        messages.append({"role": "assistant", "content": raw})
        log.info(f"LLM: {raw[:200]}")

        # Tenta extrair o primeiro JSON válido da resposta
        call = None
        # Remove blocos de código markdown
        clean = re.sub(r"```(?:json)?\n?(.*?)```", r"\1", raw, flags=re.DOTALL).strip()
        # Tenta parsear a resposta inteira
        try:
            call = json.loads(clean)
        except Exception:
            # Tenta encontrar o primeiro objeto JSON na resposta
            for match in re.finditer(r'\{[^{}]*"tool"[^{}]*\}', clean, re.DOTALL):
                try:
                    call = json.loads(match.group())
                    break
                except Exception:
                    continue

        if call is None:
            # Sem JSON válido — resposta final em texto
            return {"answer": clean, "sources": list(set(sources))}

        tool = call.get("tool")
        args = call.get("args", {})

        if tool == "done":
            return {"answer": args.get("answer", "Concluído."), "sources": list(set(sources))}

        if tool not in TOOL_FNS:
            # Ferramenta desconhecida — trata como resposta final
            return {"answer": args.get("answer", clean), "sources": list(set(sources))}

        result = TOOL_FNS[tool](args)
        log.info(f"Tool {tool} result: {str(result)[:200]}")

        if tool in ("edit_note", "create_note"):
            sources.append(args.get("note_id") or args.get("path", ""))

        messages.append({"role": "user", "content": f"Resultado de {tool}:\n{result}"})

    return {"answer": "Limite de iterações atingido.", "sources": list(set(sources))}


# ── Ações rápidas (mantidas para compatibilidade) ─────────────────────────────
def weekly_summary() -> dict:
    return ask("Gere um resumo executivo semanal com: projetos em andamento e status, riscos e bloqueios, próximos passos prioritários, stakeholders que precisam de atenção.")


def market_insights() -> dict:
    return ask("Com base nas análises e estudos registrados, identifique: principais tendências de mercado, oportunidades, riscos competitivos e gaps de conhecimento.")


def summarize_meeting(note_id: str) -> dict:
    return ask(f"Leia a nota {note_id} e gere: resumo executivo, decisões tomadas, action items com responsável e prazo, pontos que precisam de follow-up.")


def vault_review() -> dict:
    return ask(
        "Liste todas as notas do vault. Para cada nota que tiver wiki links no formato [[caminho/Nota]] sem alias, "
        "leia a nota e corrija para [[caminho/Nota|Nota]]. Também identifique correlações óbvias entre notas e adicione links onde pertinente. "
        "Reporte quais notas foram modificadas."
    )
