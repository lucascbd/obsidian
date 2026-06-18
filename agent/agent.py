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
    import time

    children = doc.get("children", [])
    if not children:
        return False

    # Divide conteúdo proporcionalmente entre os leaves existentes
    # ou usa um único leaf se houver apenas um
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

    # Re-busca o doc para garantir _rev atualizado antes do PUT final
    fresh = _couch("get", doc["_id"])
    fresh["size"]  = len(new_content.encode("utf-8"))
    fresh["mtime"] = int(time.time() * 1000)
    _couch("put", fresh["_id"], json=fresh)
    return True


# ── Tools disponíveis para o agente ──────────────────────────────────────────
def purge_vault(keep_prefix: str | None = None) -> dict:
    """Marca todas as notas ativas como deleted=true no CouchDB (formato LiveSync).
    keep_prefix: se fornecido, preserva notas que começam com esse prefixo."""
    import time
    r = requests.get(
        f"{COUCHDB_URL}/{COUCHDB_DB}/_all_docs",
        params={"include_docs": True},
        auth=COUCHDB_AUTH, timeout=30,
    )
    r.raise_for_status()
    purged, skipped = [], []
    for row in r.json().get("rows", []):
        doc = row.get("doc", {})
        nid = row["id"]
        if nid.startswith("_") or nid.startswith("h:") or nid == "obsydian_livesync_version":
            continue
        if doc.get("deleted"):
            continue
        if keep_prefix and nid.startswith(keep_prefix):
            skipped.append(nid)
            continue
        try:
            doc["deleted"] = True
            doc["mtime"]   = int(time.time() * 1000)
            _couch("put", nid, json=doc)
            purged.append(nid)
            log.info(f"Purged: {nid}")
        except Exception as e:
            log.error(f"Erro ao purgar {nid}: {e}")
    return {
        "answer": f"{len(purged)} nota(s) removida(s) do banco.\n" + "\n".join(f"- {n}" for n in purged),
        "sources": purged,
        "skipped": skipped,
    }


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

def get_root_folders() -> list:
    """Retorna as pastas raiz do vault (primeiro segmento de path das notas ativas, não deletadas)."""
    try:
        r = requests.get(
            f"{COUCHDB_URL}/{COUCHDB_DB}/_all_docs",
            params={"include_docs": True},
            auth=COUCHDB_AUTH, timeout=30,
        )
        r.raise_for_status()
        roots = set()
        for row in r.json().get("rows", []):
            nid = row["id"]
            if nid.startswith("_") or nid.startswith("h:"):
                continue
            if row.get("doc", {}).get("deleted"):
                continue
            parts = nid.split("/")
            if len(parts) > 1:
                roots.add(parts[0])
        return sorted(roots)
    except Exception as e:
        log.error(f"Erro ao listar raízes: {e}")
        return []


def tool_search_vault(query: str, collections: list | None = None, root: str | None = None) -> str:
    """Busca semântica no vault via ChromaDB, opcionalmente filtrada por pasta raiz."""
    cols  = collections or COLLECTIONS
    model = get_embed_model()
    chroma = get_chroma()
    embedding = list(model.embed([query]))[0].tolist()
    results = []
    for col_name in cols:
        try:
            col = chroma.get_collection(col_name)
            r   = col.query(query_embeddings=[embedding], n_results=10)
            for doc, meta, dist in zip(r["documents"][0], r["metadatas"][0], r["distances"][0]):
                nid = meta["note_id"]
                if root and not nid.startswith(root + "/"):
                    continue
                results.append({
                    "note_id": nid,
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


def tool_list_notes(root: str | None = None) -> str:
    """Lista todas as notas ativas (não deletadas) do vault, opcionalmente filtradas por pasta raiz."""
    try:
        r = requests.get(
            f"{COUCHDB_URL}/{COUCHDB_DB}/_all_docs",
            params={"include_docs": True},
            auth=COUCHDB_AUTH, timeout=30,
        )
        r.raise_for_status()
        ids = [
            row["id"] for row in r.json().get("rows", [])
            if not row["id"].startswith("_")
            and not row["id"].startswith("h:")
            and not row.get("doc", {}).get("deleted")
            and (not root or row["id"].startswith(root + "/"))
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


INGEST_PROMPT = """Você é o Mordomo do Conhecimento. Converta o conteúdo abaixo em uma nota Obsidian bem estruturada.

INSTRUÇÕES:
1. Analise o conteúdo e decida a pasta/subpasta mais semântica para ele.
   - Use hierarquias temáticas livres. Exemplos:
     - Um artigo sobre IA → "tecnologia/inteligencia-artificial/artigo-nome.md"
     - Uma ata de reunião → "reunioes/2024/cliente-x-kick-off.md"
     - Perfil de empresa  → "empresas/nome-empresa/perfil.md"
     - Relatório de mercado → "mercado/latam/nome-relatorio.md"
   - Seja específico. Prefira "projetos/saas-b2b/roadmap-q1.md" a "projetos/nota.md"
2. Crie slug lowercase com hifens, sem acentos.
3. Escreva a nota em markdown com:
   - Frontmatter YAML: title, date (hoje: {today}), source (se URL/arquivo), tags (3-5 relevantes)
   - Seções organizadas com # ## ###
   - Linguagem concisa em português
   - Wiki links para notas do vault que sejam relacionadas: [[caminho/nota|Nome]]
4. Se o conteúdo mencionar pessoas, empresas, projetos que não têm nota no vault, crie entradas no campo "related_notes_to_create" para que o agente crie depois.

NOTAS EXISTENTES NO VAULT:
{note_ids}

Responda APENAS com um JSON no formato:
{{"path": "pasta/subpasta/nome.md", "content": "conteúdo completo da nota", "summary": "1-2 frases descrevendo o que foi criado"}}"""


def ingest(raw_text: str, source_name: str, root: str | None = None) -> dict:
    """Converte texto bruto em nota Obsidian e salva no vault."""
    import datetime
    note_ids = tool_list_notes(root=root)
    today = datetime.date.today().isoformat()

    root_instruction = (
        f"\nESCOPO: A nota DEVE ser criada dentro de '{root}/'. "
        f"O path deve começar com '{root}/'. "
        f"NUNCA crie wiki links para notas fora de '{root}/'."
    ) if root else ""

    prompt = (INGEST_PROMPT + root_instruction).replace("{note_ids}", note_ids[:3000]).replace("{today}", today)
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

        # Garante que o path respeita o root
        if root and not path.startswith(root + "/"):
            path = f"{root}/{path}"

        result = tool_create_note(path, content)
        log.info(f"Ingest: {result}")

        return {
            "answer":  f"{summary}\n\nNota criada: `{path}`\n\n{result}",
            "sources": [path],
            "path":    path,
        }

    return {"answer": "Falha ao processar o conteúdo após 3 tentativas.", "sources": []}


def ingest_file(filename: str, content: bytes, root: str | None = None) -> dict:
    raw_text = _extract_text_from_file(filename, content)
    return ingest(raw_text, source_name=filename, root=root)


def ingest_url(url: str, root: str | None = None) -> dict:
    try:
        raw_text = _extract_text_from_url(url)
    except Exception as e:
        return {"answer": f"Erro ao buscar URL: {e}", "sources": []}
    return ingest(raw_text, source_name=url, root=root)


def ingest_zip(content: bytes, root: str | None = None) -> dict:
    """Extrai um ZIP e ingere cada arquivo suportado como nota Obsidian."""
    import zipfile
    SUPPORTED = {"txt", "md", "html", "htm", "pdf", "docx"}
    created, errors = [], []

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            # Filtra apenas arquivos suportados
            names = [n for n in names if (n.rsplit(".", 1)[-1].lower() if "." in n else "") in SUPPORTED]
            log.info(f"ZIP: {len(names)} arquivos suportados para ingestão")
            for name in names:
                try:
                    file_bytes = zf.read(name)
                    basename   = os.path.basename(name)
                    result     = ingest_file(basename, file_bytes, root=root)
                    created.append(result.get("path", basename))
                    log.info(f"ZIP ingest OK: {result.get('path')}")
                except Exception as e:
                    errors.append(f"{name}: {e}")
                    log.error(f"ZIP ingest erro {name}: {e}")
    except zipfile.BadZipFile:
        return {"answer": "Arquivo ZIP inválido ou corrompido.", "sources": []}

    lines = [f"**{len(created)} notas criadas** de {len(names)} arquivos no ZIP"]
    if root:
        lines.append(f"Escopo: `{root}/`")
    lines.append("")
    lines.extend(f"- `{p}`" for p in created)
    if errors:
        lines.append(f"\n**{len(errors)} erros:**")
        lines.extend(f"- {e}" for e in errors)

    return {"answer": "\n".join(lines), "sources": created}


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

SYSTEM_PROMPT = """Você é o Mordomo do Conhecimento — um agente autônomo que organiza, conecta e enriquece o vault Obsidian do usuário.

MISSÃO:
Transformar informações brutas em conhecimento navegável. Você decide a estrutura de pastas, cria notas, estabelece conexões e mantém o vault sempre organizado e coerente. Você age como um mordomo inteligente: antecipa necessidades, organiza sem pedir permissão, e executa tarefas até o fim.

ESTRUTURA DE PASTAS — LIVRE:
- Você pode criar qualquer pasta e subpasta que faça sentido para o contexto
- Exemplos válidos: "clientes/acme/projetos/", "mercado/latam/analises/", "pessoas/equipe/", "produtos/roadmap/"
- Prefira hierarquias semânticas ao invés de números: "projetos/nome-do-projeto/" ao invés de "01-projetos/"
- Crie pastas temáticas conforme o conteúdo cresce
- Use slugs em lowercase com hifens, sem acentos: "reuniao-kick-off.md"

FORMATO OBRIGATÓRIO — SEM EXCEÇÃO:
- Responda SEMPRE com exatamente um objeto JSON por mensagem
- NUNCA misture texto com JSON
- NUNCA escreva explicações fora do JSON
- Use SOMENTE estes formatos:

{"tool": "search_vault", "args": {"query": "...", "collections": ["opcional"]}}
{"tool": "read_note", "args": {"note_id": "caminho/nota.md"}}
{"tool": "list_notes", "args": {}}
{"tool": "edit_note", "args": {"note_id": "caminho/nota.md", "content": "conteúdo markdown completo"}}
{"tool": "create_note", "args": {"path": "pasta/subpasta/nota.md", "content": "conteúdo markdown completo"}}
{"tool": "done", "args": {"answer": "resumo completo do que foi feito"}}

REGRAS DE KNOWLEDGE GRAPH:
- Wiki links SEMPRE com alias: [[caminho/nota|Nome Visível]]
- Exemplo correto: [[clientes/acme/perfil|ACME]]
- Exemplo errado: [[clientes/acme/perfil]] ou [[ACME]]
- Preserve frontmatter YAML ao editar notas
- Adicione links onde houver correlação real — pessoas, projetos, temas, empresas
- Use search_vault (ChromaDB) para descobrir correlações antes de criar links
- Frontmatter mínimo: title, date, tags

COMPORTAMENTO:
- Execute a tarefa completa sem parar no meio
- Se uma subtarefa falhar, continue com as próximas
- Ao criar uma nota, sempre busque (search_vault) notas relacionadas para adicionar wiki links
- No "done", liste todas as notas criadas/editadas com uma linha de descrição cada"""


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


def ask(question: str, collections: list | None = None, root: str | None = None) -> dict:
    """Loop de tool calling via prompt até o agente chamar 'done'."""
    root_ctx = (
        f"\n\nESCOPO ATIVO: '{root}/'\n"
        f"- Todas as notas criadas DEVEM começar com '{root}/'\n"
        f"- list_notes e search_vault já retornam apenas notas de '{root}/'\n"
        f"- NUNCA crie wiki links apontando para fora de '{root}/'\n"
        f"- Ao criar uma nota, se o path não começar com '{root}/', adicione automaticamente"
    ) if root else ""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + root_ctx},
        {"role": "user",   "content": question},
    ]
    sources = []

    # Ferramentas com escopo de root aplicado
    def _scoped_search(args):
        return tool_search_vault(root=root, **args)

    def _scoped_list(_args):
        return tool_list_notes(root=root)

    def _scoped_read(args):
        nid = args.get("note_id", "")
        if root and not nid.startswith(root + "/"):
            return f"Bloqueado: nota '{nid}' está fora do escopo '{root}/'."
        return tool_read_note(**args)

    def _scoped_edit(args):
        nid = args.get("note_id", "")
        if root and not nid.startswith(root + "/"):
            return f"Bloqueado: nota '{nid}' está fora do escopo '{root}/'."
        return tool_edit_note(**args)

    def _scoped_create(args):
        if root:
            path = args.get("path", "")
            if not path.startswith(root + "/"):
                args = {**args, "path": f"{root}/{path}"}
        return tool_create_note(**args)

    scoped_fns = {
        "search_vault": _scoped_search,
        "read_note":    _scoped_read,
        "list_notes":   _scoped_list,
        "edit_note":    _scoped_edit,
        "create_note":  _scoped_create,
    }

    bad_format_streak = 0
    while True:
        raw = _call_llm_raw(messages)
        messages.append({"role": "assistant", "content": raw})
        log.info(f"LLM: {raw[:200]}")

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
            bad_format_streak += 1
            if bad_format_streak >= 5:
                return {"answer": "Agente travou em loop de formato inválido após 5 tentativas consecutivas.", "sources": list(set(sources))}
            messages.append({"role": "user", "content": "Responda APENAS com um JSON válido. Nenhum texto antes ou depois."})
            continue

        bad_format_streak = 0
        tool = call.get("tool")
        args = call.get("args", {})

        if tool == "done":
            return {"answer": args.get("answer", "Concluído."), "sources": list(set(sources))}

        if tool not in scoped_fns:
            messages.append({"role": "user", "content": f"Ferramenta '{tool}' não existe. Use: search_vault, read_note, list_notes, edit_note, create_note, done."})
            continue

        result = scoped_fns[tool](args)
        log.info(f"Tool {tool} result: {str(result)[:200]}")

        if tool in ("edit_note", "create_note"):
            sources.append(args.get("note_id") or args.get("path", ""))

        messages.append({"role": "user", "content": f"Resultado de {tool}:\n{result}"})


# ── Ações rápidas (mantidas para compatibilidade) ─────────────────────────────
def weekly_summary(root: str | None = None) -> dict:
    return ask("Gere um resumo executivo semanal com: projetos em andamento e status, riscos e bloqueios, próximos passos prioritários, stakeholders que precisam de atenção.", root=root)


def market_insights(root: str | None = None) -> dict:
    return ask("Com base nas análises e estudos registrados, identifique: principais tendências de mercado, oportunidades, riscos competitivos e gaps de conhecimento.", root=root)


def summarize_meeting(note_id: str, root: str | None = None) -> dict:
    return ask(f"Leia a nota {note_id} e gere: resumo executivo, decisões tomadas, action items com responsável e prazo, pontos que precisam de follow-up.", root=root)


def vault_review(root: str | None = None) -> dict:
    return ask(
        "Liste todas as notas do vault. Para cada nota que tiver wiki links no formato [[caminho/Nota]] sem alias, "
        "leia a nota e corrija para [[caminho/Nota|Nota]]. Também identifique correlações óbvias entre notas e adicione links onde pertinente. "
        "Reporte quais notas foram modificadas.",
        root=root,
    )
