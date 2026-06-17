"""
agent.py — Agente com tool calling: lê e escreve no vault via CouchDB/ChromaDB.
"""

import os
import re
import io
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


# ── Ingestão de arquivos e URLs ──────────────────────────────────────────────
def _extract_text_from_file(filename: str, content: bytes) -> str:
    """Extrai texto de PDF, DOCX, TXT, MD ou HTML."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if ext in ("docx",):
        from docx import Document
        doc = Document(io.BytesIO(content))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if ext in ("html", "htm"):
        from bs4 import BeautifulSoup
        import markdownify
        soup = BeautifulSoup(content, "html.parser")
        return markdownify.markdownify(str(soup), heading_style="ATX")
    # txt, md, csv, json — trata como texto plano
    return content.decode("utf-8", errors="replace")


def _extract_text_from_url(url: str) -> str:
    """Busca URL e extrai texto em markdown."""
    import markdownify
    from bs4 import BeautifulSoup
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if "pdf" in ct:
        return _extract_text_from_file("page.pdf", r.content)
    soup = BeautifulSoup(r.content, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    return markdownify.markdownify(str(main), heading_style="ATX")


INGEST_PROMPT = """Você é um assistente que converte conteúdo bruto em notas Obsidian bem estruturadas.

Dado o conteúdo extraído abaixo, você deve:
1. Escolher a pasta correta:
   - 00-inbox        → rascunhos, conteúdo não categorizado
   - 01-projetos     → projetos, iniciativas, planos
   - 02-analises     → análises, estudos, relatórios
   - 03-stakeholders → pessoas, empresas, parceiros
   - 04-referencias  → artigos, livros, fontes externas
   - 05-reunioes     → atas de reunião, encontros
2. Criar um slug de nome de arquivo (lowercase, hifens, sem acentos, .md)
3. Escrever a nota em markdown com:
   - Frontmatter YAML: title, date (hoje), source (se URL), tags
   - Seções bem organizadas com # ## ###
   - Linguagem concisa em português
4. Ao final, liste wiki links para notas existentes no vault que sejam relacionadas.

NOTAS EXISTENTES NO VAULT:
{note_ids}

Responda APENAS com um JSON no formato:
{{"path": "01-projetos/nome-do-arquivo.md", "content": "conteúdo completo da nota", "summary": "1-2 frases descrevendo o que foi criado"}}"""


def ingest(raw_text: str, source_name: str) -> dict:
    """Converte texto bruto em nota Obsidian e salva no vault."""
    note_ids = tool_list_notes()

    prompt = INGEST_PROMPT.replace("{note_ids}", note_ids[:3000])
    user_msg = f"FONTE: {source_name}\n\nCONTEÚDO:\n{raw_text[:8000]}"

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user",   "content": user_msg},
    ]

    for attempt in range(3):
        raw = _call_llm_raw(messages)
        clean = re.sub(r"```(?:json)?\n?(.*?)```", r"\1", raw, flags=re.DOTALL).strip()
        try:
            data = json.loads(clean)
        except Exception:
            for match in re.finditer(r'\{(?:[^{}]|\{[^{}]*\})*\}', clean, re.DOTALL):
                try:
                    data = json.loads(match.group())
                    break
                except Exception:
                    pass
            else:
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "Responda APENAS com um JSON válido conforme o formato solicitado."})
                continue

        path    = data.get("path", "00-inbox/imported.md")
        content = data.get("content", raw_text[:4000])
        summary = data.get("summary", f"Nota criada a partir de {source_name}")

        result = tool_create_note(path, content)
        log.info(f"Ingest: {result}")

        # Dispara busca de correlações e adiciona links em segundo plano (via ask)
        return {
            "answer":  f"{summary}\n\nNota criada: `{path}`\n\n{result}",
            "sources": [path],
            "path":    path,
        }

    return {"answer": "Falha ao processar o conteúdo após 3 tentativas.", "sources": []}


def ingest_file(filename: str, content: bytes) -> dict:
    raw_text = _extract_text_from_file(filename, content)
    return ingest(raw_text, source_name=filename)


def ingest_url(url: str) -> dict:
    try:
        raw_text = _extract_text_from_url(url)
    except Exception as e:
        return {"answer": f"Erro ao buscar URL: {e}", "sources": []}
    return ingest(raw_text, source_name=url)


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

SYSTEM_PROMPT = """Você é o Knowledge Graph Agent do Obsidian. Sua única função é construir, enriquecer e manter a rede de conhecimento do vault.

IDENTIDADE:
- Você conecta ideias, pessoas, projetos e fontes através de wiki links
- Você enriquece notas com contexto, referências cruzadas e correlações
- Você organiza o conhecimento para que seja navegável e útil

FORMATO OBRIGATÓRIO:
- Responda SEMPRE com exatamente um objeto JSON por mensagem
- NUNCA misture texto com JSON
- NUNCA escreva explicações fora do JSON
- Use SOMENTE estes formatos:

{"tool": "search_vault", "args": {"query": "...", "collections": ["opcional"]}}
{"tool": "read_note", "args": {"note_id": "caminho/nota.md"}}
{"tool": "list_notes", "args": {}}
{"tool": "edit_note", "args": {"note_id": "caminho/nota.md", "content": "conteúdo markdown completo"}}
{"tool": "create_note", "args": {"path": "caminho/nota.md", "content": "conteúdo markdown completo"}}
{"tool": "done", "args": {"answer": "resumo do que foi feito"}}

REGRAS DE KNOWLEDGE GRAPH:
- Wiki links SEMPRE com alias: [[caminho/nota|Nome Visível]]
- Exemplo correto: [[05-fontes/Nielsen|Nielsen]]
- Exemplo errado: [[05-fontes/Nielsen]] ou [[Nielsen]]
- Preserve frontmatter YAML ao editar notas
- Adicione links onde houver correlação real, não forçada
- Use search_vault (ChromaDB) para encontrar notas semanticamente relacionadas antes de criar links
- Ao terminar uma tarefa longa, resuma TODAS as notas modificadas no "done"

FLUXO PARA REVISÃO DE VAULT:
1. list_notes → obter todos os IDs
2. Para cada nota: read_note → identificar menções implícitas → search_vault para confirmar correlações → edit_note com links adicionados
3. done com resumo completo

NUNCA pare no meio. Complete a tarefa inteira antes de chamar "done"."""


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

        # Extrai JSON da resposta — ignora qualquer texto ao redor
        call = None
        clean = re.sub(r"```(?:json)?\n?(.*?)```", r"\1", raw, flags=re.DOTALL).strip()

        try:
            call = json.loads(clean)
        except Exception:
            for match in re.finditer(r'\{(?:[^{}]|\{[^{}]*\})*\}', clean, re.DOTALL):
                try:
                    candidate = json.loads(match.group())
                    if "tool" in candidate:
                        call = candidate
                        break
                except Exception:
                    continue

        if call is None:
            messages.append({"role": "user", "content": "Responda APENAS com um JSON válido. Nenhum texto antes ou depois."})
            continue

        tool = call.get("tool")
        args = call.get("args", {})

        if tool == "done":
            return {"answer": args.get("answer", "Concluído."), "sources": list(set(sources))}

        if tool not in TOOL_FNS:
            messages.append({"role": "user", "content": f"Ferramenta '{tool}' não existe. Use: search_vault, read_note, list_notes, edit_note, create_note, done."})
            continue

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
