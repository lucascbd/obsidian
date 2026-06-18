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
from typing import Optional
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

COLLECTIONS = []  # dinâmico: populado a partir das coleções existentes no ChromaDB

import re as _re
_INVALID_COL = _re.compile(r"[^a-zA-Z0-9_-]")


def _col_name(note_id: str) -> str:
    root = note_id.split("/")[0] if "/" in note_id else "vault"
    return (_INVALID_COL.sub("_", root)[:63]) or "vault"


def _get_all_collections() -> list:
    """Retorna todas as coleções existentes no ChromaDB."""
    try:
        return [c.name for c in get_chroma().list_collections()]
    except Exception:
        return []

MAX_ROUNDS = 200

_embed_model = None
_chroma      = None

# Palavras-chave que indicam trabalho pendente na resposta de "done"
_PENDING_PATTERNS = [
    r"preciso continuar",
    r"ainda restam",
    r"pr[oó]ximas? notas?",
    r"continuarei",
    r"continuando",
    r"processar(ei)? as? (pr[oó]ximas?|demais|restantes)",
    r"nas pr[oó]ximas? itera[cç][oõ]es?",
    r"faltam \d+",
    r"pendentes?",
]
_PENDING_RE = re.compile("|".join(_PENDING_PATTERNS), re.IGNORECASE)


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


# ── Sistema 00-settings/ ──────────────────────────────────────────────────────
DEFAULT_PROFILE_MD = """# Perfil do Agente

Você é o Mordomo do Conhecimento — um agente autônomo que organiza, conecta e enriquece o vault Obsidian do usuário.

## Persona
- Organizado, proativo e direto
- Executa tarefas até o fim sem pedir confirmação desnecessária
- Usa linguagem clara e objetiva em português

## Comportamento
- Antecipa necessidades do usuário
- Mantém consistência estrutural no vault
- Prefere ação a análise excessiva
"""

DEFAULT_LOAD_MD = """# Regras de Ingestão

## Estrutura de Pastas
- Artigos e referências → `referencias/tema/nome.md`
- Reuniões e atas → `reunioes/YYYY/nome-reuniao.md`
- Projetos → `projetos/nome-projeto/`
- Análises e estudos → `analises/tema/nome.md`
- Perfis de empresas → `empresas/nome-empresa/perfil.md`
- Relatórios de mercado → `mercado/regiao/nome-relatorio.md`

## Frontmatter Obrigatório
```yaml
---
title: Título da Nota
date: YYYY-MM-DD
source: URL ou nome do arquivo
tags:
  - tag1
  - tag2
---
```

## Qualidade
- Slug em lowercase com hifens, sem acentos
- Seções organizadas com ## e ###
- Wiki links com alias: [[caminho/nota|Nome Visível]]
- Conteúdo em português, conciso e navegável
"""


def load_settings(root: str) -> dict:
    """Lê as notas de settings do vault. Retorna dict com profile, load e mem."""
    settings = {"profile": "", "load": "", "mem": ""}
    mapping = {
        "profile": f"{root}/00-settings/profile.md",
        "load":    f"{root}/00-settings/load.md",
        "mem":     f"{root}/00-settings/agent-mem.md",
    }
    for key, note_id in mapping.items():
        try:
            doc = _couch("get", note_id)
            if not doc.get("deleted"):
                content = _read_note_content(doc)
                settings[key] = content
        except Exception:
            pass
    return settings


def save_session_memory(root: str, question: str, answer: str, sources: list, actions_summary: str) -> None:
    """Faz append em {root}/00-settings/agent-mem.md com o resumo da sessão."""
    import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M") + " UTC"
    note_id = f"{root}/00-settings/agent-mem.md"

    # Bullet points das notas afetadas
    notes_bullets = "\n".join(f"- {s}" for s in sources) if sources else "- (nenhuma nota afetada)"

    new_entry = (
        f"\n## Sessão {now}\n"
        f"**Tarefa:** {question}\n"
        f"**Ações:** {actions_summary}\n"
        f"**Notas afetadas:**\n{notes_bullets}\n"
        f"---\n"
    )

    try:
        doc = _couch("get", note_id)
        if doc.get("deleted"):
            raise Exception("nota deletada")
        existing = _read_note_content(doc)
        _write_note_content(doc, existing + new_entry)
    except Exception:
        # Cria a nota se não existir
        tool_create_note(note_id, f"# Memória de Sessões\n{new_entry}")


def tool_ensure_settings(root: str) -> str:
    """Cria {root}/00-settings/profile.md e {root}/00-settings/load.md com conteúdo padrão se não existirem."""
    created = []
    for note_id, default_content in [
        (f"{root}/00-settings/profile.md", DEFAULT_PROFILE_MD),
        (f"{root}/00-settings/load.md",    DEFAULT_LOAD_MD),
    ]:
        try:
            doc = _couch("get", note_id)
            if not doc.get("deleted"):
                continue  # já existe
        except Exception:
            pass
        result = tool_create_note(note_id, default_content)
        created.append(f"{note_id}: {result}")

    if not created:
        return f"Arquivos de settings já existem em '{root}/00-settings/'."
    return "Settings criados:\n" + "\n".join(created)


# ── Tools disponíveis para o agente ──────────────────────────────────────────
def purge_vault(keep_prefix: Optional[str] = None) -> dict:
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


def purge_orphan_leaves() -> dict:
    """Remove leaf docs (h:) cujo pai está deletado ou não existe."""
    r = requests.get(
        f"{COUCHDB_URL}/{COUCHDB_DB}/_all_docs",
        params={"include_docs": True},
        auth=COUCHDB_AUTH, timeout=30,
    )
    r.raise_for_status()
    rows = r.json().get("rows", [])

    # Coleta todos os leaf IDs referenciados por notas ativas
    referenced = set()
    for row in rows:
        doc = row.get("doc", {})
        if doc.get("deleted") or row["id"].startswith("h:") or row["id"].startswith("_"):
            continue
        referenced.update(doc.get("children", []))

    # Deleta leaves não referenciados
    removed = []
    for row in rows:
        nid = row["id"]
        if not nid.startswith("h:"):
            continue
        if nid in referenced:
            continue
        try:
            doc = row["doc"]
            requests.delete(
                f"{COUCHDB_URL}/{COUCHDB_DB}/{requests.utils.quote(nid, safe='')}",
                params={"rev": doc["_rev"]},
                auth=COUCHDB_AUTH, timeout=10,
            ).raise_for_status()
            removed.append(nid)
        except Exception as e:
            log.error(f"Erro ao deletar leaf {nid}: {e}")

    return {
        "answer": f"{len(removed)} leaf(s) órfão(s) removido(s).",
        "sources": [],
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
    """Retorna as pastas raiz do vault (primeiro segmento de path das notas ativas)."""
    try:
        r = requests.get(
            f"{COUCHDB_URL}/{COUCHDB_DB}/_all_docs",
            auth=COUCHDB_AUTH, timeout=30,
        )
        r.raise_for_status()
        roots = set()
        for row in r.json().get("rows", []):
            nid = row["id"]
            if nid.startswith("_") or nid.startswith("h:"):
                continue
            parts = nid.split("/")
            if len(parts) > 1:
                roots.add(parts[0])
        log.info(f"get_root_folders found: {sorted(roots)}")
        return sorted(roots)
    except Exception as e:
        log.error(f"Erro ao listar raízes: {e}")
        return []


def _is_settings_note(note_id: str) -> bool:
    """Retorna True se a nota pertence à pasta 00-settings (em qualquer root)."""
    parts = note_id.split("/")
    return "00-settings" in parts


def tool_search_vault(query: str, collections: Optional[list] = None, root: Optional[str] = None) -> str:
    """Busca semântica no vault via ChromaDB, opcionalmente filtrada por pasta raiz."""
    cols  = collections or _get_all_collections()
    if not cols:
        return "ChromaDB sem coleções — vault ainda não indexado pelo watcher."
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
                if _is_settings_note(nid):
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


def tool_list_notes(root: Optional[str] = None) -> str:
    """Lista todas as notas ativas (não deletadas) do vault, opcionalmente filtradas por pasta raiz.
    Exclui notas da pasta 00-settings."""
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
            and not _is_settings_note(row["id"])
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


def ingest(raw_text: str, source_name: str, root: Optional[str] = None) -> dict:
    """Converte texto bruto em nota Obsidian e salva no vault."""
    import datetime
    note_ids = tool_list_notes(root=root)
    today = datetime.date.today().isoformat()

    root_instruction = (
        f"\nESCOPO: A nota DEVE ser criada dentro de '{root}/'. "
        f"O path deve começar com '{root}/'. "
        f"NUNCA crie wiki links para notas fora de '{root}/'."
    ) if root else ""

    # Carrega regras de ingestão do vault se root fornecido
    load_rules = ""
    if root:
        settings = load_settings(root)
        if settings.get("load"):
            load_rules = f"\n\nREGRAS DE INGESTÃO DO VAULT:\n{settings['load']}"

    prompt = (INGEST_PROMPT + root_instruction + load_rules).replace("{note_ids}", note_ids[:3000]).replace("{today}", today)
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


def ingest_file(filename: str, content: bytes, root: Optional[str] = None) -> dict:
    raw_text = _extract_text_from_file(filename, content)
    return ingest(raw_text, source_name=filename, root=root)


def ingest_url(url: str, root: Optional[str] = None) -> dict:
    try:
        raw_text = _extract_text_from_url(url)
    except Exception as e:
        return {"answer": f"Erro ao buscar URL: {e}", "sources": []}
    return ingest(raw_text, source_name=url, root=root)


def ingest_zip(content: bytes, root: Optional[str] = None) -> dict:
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
def tool_move_note(source_id: str, dest_id: str) -> str:
    """Move/renomeia uma nota: copia conteúdo e leaves para o novo ID e marca o original como deleted."""
    import time, hashlib
    try:
        src_doc = _couch("get", source_id)
        if src_doc.get("deleted"):
            return f"Nota '{source_id}' já está deletada."
        content = _read_note_content(src_doc)

        # Cria novo leaf e doc no destino
        leaf_id = "h:" + hashlib.md5(f"{dest_id}{time.time()}".encode()).hexdigest()[:13]
        leaf = {"_id": leaf_id, "data": content, "type": "leaf"}
        doc  = {
            "_id":      dest_id,
            "children": [leaf_id],
            "path":     dest_id,
            "ctime":    src_doc.get("ctime", int(time.time() * 1000)),
            "mtime":    int(time.time() * 1000),
            "size":     len(content.encode("utf-8")),
            "type":     "plain",
            "eden":     {},
        }
        _couch("put", leaf_id, json=leaf)
        _couch("put", dest_id, json=doc)

        # Marca original como deleted (formato LiveSync)
        src_doc["deleted"] = True
        src_doc["mtime"]   = int(time.time() * 1000)
        _couch("put", source_id, json=src_doc)

        log.info(f"move_note: {source_id} → {dest_id}")
        return f"Nota movida: '{source_id}' → '{dest_id}'"
    except Exception as e:
        return f"Erro ao mover nota: {e}"


def tool_delete_note(note_id: str) -> str:
    """Marca uma nota como deleted=true no CouchDB (formato LiveSync). Use apenas para duplicatas confirmadas."""
    import time
    try:
        doc = _couch("get", note_id)
        if doc.get("deleted"):
            return f"Nota '{note_id}' já estava deletada."
        doc["deleted"] = True
        doc["mtime"]   = int(time.time() * 1000)
        _couch("put", note_id, json=doc)
        log.info(f"delete_note: {note_id}")
        return f"Nota '{note_id}' deletada."
    except Exception as e:
        return f"Erro ao deletar nota '{note_id}': {e}"


def tool_add_tags(note_id: str, tags: list[str]) -> str:
    """Adiciona tags ao frontmatter YAML de uma nota. Cria o bloco --- se não existir. Não duplica tags já presentes."""
    try:
        doc = _couch("get", note_id)
        content = _read_note_content(doc)

        # Extrai frontmatter
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end == -1:
                # Sem fechamento — adiciona frontmatter do zero antes do conteúdo
                existing_tags = []
                fm = ""
                body = content
            else:
                fm = content[3:end]
                body = content[end + 4:]  # após o ---\n de fechamento
                # Extrai tags já existentes
                existing_tags = re.findall(r"^\s*-\s+(\S+)", fm.split("tags:", 1)[-1].split("\n\n")[0], re.MULTILINE) if "tags:" in fm else []
        else:
            fm = ""
            body = content
            existing_tags = []

        new_tags = [t for t in tags if t not in existing_tags]
        if not new_tags:
            return f"Nenhuma tag nova para adicionar em '{note_id}' (já existem: {existing_tags})."

        tags_yaml = "\n".join(f"  - {t}" for t in new_tags)

        if fm and "tags:" in fm:
            # Insere novas tags logo após o último item existente da lista
            updated_fm = re.sub(
                r"(tags:(?:\n  - \S+)*)",
                lambda m: m.group(0) + "\n" + tags_yaml,
                fm,
            )
        elif fm:
            updated_fm = fm.rstrip() + f"\ntags:\n{tags_yaml}\n"
        else:
            updated_fm = f"tags:\n{tags_yaml}\n"

        new_content = f"---\n{updated_fm}\n---\n{body}"
        ok = _write_note_content(doc, new_content)
        if not ok:
            return f"Falha ao salvar tags em '{note_id}'."
        log.info(f"add_tags: {note_id} +{new_tags}")
        return f"Tags adicionadas em '{note_id}': {new_tags}"
    except Exception as e:
        return f"Erro ao adicionar tags em '{note_id}': {e}"


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
    {
        "type": "function",
        "function": {
            "name": "move_note",
            "description": "Move ou renomeia uma nota. Copia o conteúdo para o novo caminho e marca o original como deletado. Use para reorganizar pastas ou corrigir nomes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string", "description": "Caminho atual da nota"},
                    "dest_id":   {"type": "string", "description": "Novo caminho da nota"},
                },
                "required": ["source_id", "dest_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_note",
            "description": "Deleta permanentemente uma nota. Use SOMENTE para duplicatas confirmadas ou notas que o usuário pediu para remover.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "Caminho da nota a deletar"},
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_tags",
            "description": "Adiciona tags ao frontmatter YAML de uma nota sem sobrescrever o conteúdo. Mais seguro que edit_note para mudanças só de tags.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "Caminho da nota"},
                    "tags":    {"type": "array", "items": {"type": "string"}, "description": "Tags a adicionar, ex: ['politica', 'analise', 'brasil']"},
                },
                "required": ["note_id", "tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ensure_settings",
            "description": "Cria os arquivos de configuração do agente (profile.md e load.md) em 00-settings/ se não existirem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root": {"type": "string", "description": "Pasta raiz do vault"},
                },
                "required": ["root"],
            },
        },
    },
]

TOOL_FNS = {
    "search_vault":   lambda args: tool_search_vault(**args),
    "read_note":      lambda args: tool_read_note(**args),
    "list_notes":     lambda args: tool_list_notes(),
    "edit_note":      lambda args: tool_edit_note(**args),
    "create_note":    lambda args: tool_create_note(**args),
    "move_note":      lambda args: tool_move_note(**args),
    "delete_note":    lambda args: tool_delete_note(**args),
    "add_tags":       lambda args: tool_add_tags(**args),
    "ensure_settings": lambda args: tool_ensure_settings(**args),
}

SYSTEM_PROMPT = """Você é o Mordomo do Conhecimento — um agente autônomo que organiza, conecta e enriquece o vault Obsidian do usuário.

MISSÃO:
Transformar informações brutas em conhecimento navegável. Você decide a estrutura de pastas, cria notas, estabelece conexões e mantém o vault sempre organizado e coerente. Você age como um mordomo inteligente: antecipa necessidades, organiza sem pedir permissão, e executa tarefas até o fim — sem parar no meio, sem pedir confirmação.

ESTRUTURA DE PASTAS — LIVRE:
- Você pode criar qualquer pasta e subpasta que faça sentido para o contexto
- Prefira hierarquias semânticas: "projetos/nome-do-projeto/" ao invés de "01-projetos/"
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
{"tool": "move_note", "args": {"source_id": "caminho/atual.md", "dest_id": "caminho/novo.md"}}
{"tool": "delete_note", "args": {"note_id": "caminho/nota.md"}}
{"tool": "add_tags", "args": {"note_id": "caminho/nota.md", "tags": ["tag1", "tag2"]}}
{"tool": "ensure_settings", "args": {"root": "nome-do-vault"}}
{"tool": "done", "args": {"answer": "resumo completo do que foi feito"}}

REGRAS DE EXECUÇÃO:
- Execute a tarefa COMPLETA até o fim. Se há 50 notas para processar, processe as 50.
- Não pare no meio para "listar pendências" — execute as pendências.
- Se uma subtarefa falhar, continue com as próximas sem parar.
- Ao reorganizar pastas: use move_note (não criar + deletar manualmente).
- Ao padronizar tags: use add_tags (mais seguro que edit_note para só adicionar tags).
- Ao deletar duplicatas: confirme que o conteúdo foi movido antes de usar delete_note.

REGRAS DE KNOWLEDGE GRAPH:
- Wiki links SEMPRE com alias: [[caminho/nota|Nome Visível]]
- Preserve frontmatter YAML ao editar notas
- Frontmatter mínimo: title, date, tags

CONCLUSÃO:
- Só chame "done" quando TUDO estiver feito
- No "done", liste cada nota movida/editada/criada/deletada com uma linha de descrição"""


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
    msg = r.json()["choices"][0]["message"]
    content = msg.get("content") or ""
    # Alguns modelos retornam content=null com tool_calls — extrai o JSON do tool call
    if not content and msg.get("tool_calls"):
        tc = msg["tool_calls"][0]
        fn = tc.get("function", {})
        content = json.dumps({"tool": fn.get("name"), "args": json.loads(fn.get("arguments", "{}"))})
    return content.strip()


def ask(question: str, collections: Optional[list] = None, root: Optional[str] = None,
        on_progress=None) -> dict:
    """Loop de tool calling via prompt até o agente chamar 'done'."""

    # Carrega settings do vault e injeta no system prompt
    settings_ctx = ""
    if root:
        settings = load_settings(root)
        parts = []
        if settings.get("profile"):
            parts.append(f"## PERFIL DO AGENTE (do vault)\n{settings['profile']}")
        if settings.get("mem"):
            parts.append(f"## MEMÓRIA DE SESSÕES ANTERIORES\n{settings['mem'][-3000:]}")
        if parts:
            settings_ctx = "\n\n" + "\n\n".join(parts)

    root_ctx = (
        f"\n\nESCOPO ATIVO: '{root}/'\n"
        f"- Todas as notas criadas DEVEM começar com '{root}/'\n"
        f"- list_notes e search_vault já retornam apenas notas de '{root}/'\n"
        f"- NUNCA crie wiki links apontando para fora de '{root}/'\n"
        f"- Ao criar uma nota, se o path não começar com '{root}/', adicione automaticamente"
    ) if root else ""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + settings_ctx + root_ctx},
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

    def _scoped_move(args):
        src = args.get("source_id", "")
        dst = args.get("dest_id", "")
        if root:
            if not src.startswith(root + "/"):
                return f"Bloqueado: source '{src}' fora do escopo '{root}/'."
            if not dst.startswith(root + "/"):
                args = {**args, "dest_id": f"{root}/{dst}"}
        return tool_move_note(**args)

    def _scoped_delete(args):
        nid = args.get("note_id", "")
        if root and not nid.startswith(root + "/"):
            return f"Bloqueado: nota '{nid}' fora do escopo '{root}/'."
        return tool_delete_note(**args)

    def _scoped_add_tags(args):
        nid = args.get("note_id", "")
        if root and not nid.startswith(root + "/"):
            return f"Bloqueado: nota '{nid}' fora do escopo '{root}/'."
        return tool_add_tags(**args)

    def _scoped_ensure_settings(args):
        r = args.get("root") or root
        if not r:
            return "Erro: root não especificado."
        return tool_ensure_settings(r)

    scoped_fns = {
        "search_vault":    _scoped_search,
        "read_note":       _scoped_read,
        "list_notes":      _scoped_list,
        "edit_note":       _scoped_edit,
        "create_note":     _scoped_create,
        "move_note":       _scoped_move,
        "delete_note":     _scoped_delete,
        "add_tags":        _scoped_add_tags,
        "ensure_settings": _scoped_ensure_settings,
    }

    bad_format_streak = 0
    rounds = 0

    while rounds < MAX_ROUNDS:
        rounds += 1
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
            answer = args.get("answer", "Concluído.")

            # Auto-continue: verifica se há trabalho pendente mencionado na resposta
            if _PENDING_RE.search(answer):
                log.info("Auto-continue: trabalho pendente detectado, continuando loop.")
                messages.append({
                    "role": "user",
                    "content": "Continue a tarefa. Execute TUDO que está pendente antes de chamar done."
                })
                continue

            # Salva memória da sessão
            if root:
                try:
                    actions_summary = _build_actions_summary(sources, messages)
                    save_session_memory(root, question, answer, list(set(sources)), actions_summary)
                except Exception as e:
                    log.warning(f"Falha ao salvar memória de sessão: {e}")

            return {"answer": answer, "sources": list(set(sources))}

        if tool not in scoped_fns:
            messages.append({"role": "user", "content": f"Ferramenta '{tool}' não existe. Use: search_vault, read_note, list_notes, edit_note, create_note, done."})
            continue

        result = scoped_fns[tool](args)
        log.info(f"Tool {tool} result: {str(result)[:200]}")

        # Notifica progresso via callback
        if on_progress is not None:
            try:
                result_str = str(result)
                args_resumido = {k: (v[:80] if isinstance(v, str) and len(v) > 80 else v)
                                 for k, v in args.items()}
                on_progress({
                    "tool":           tool,
                    "args":           args_resumido,
                    "result_preview": result_str[:120],
                })
            except Exception as e:
                log.warning(f"on_progress callback erro: {e}")

        if tool in ("edit_note", "create_note", "add_tags", "ensure_settings"):
            sources.append(args.get("note_id") or args.get("path") or args.get("root", ""))
        elif tool == "move_note":
            sources.append(args.get("dest_id", ""))
        elif tool == "delete_note":
            sources.append(args.get("note_id", ""))

        messages.append({"role": "user", "content": f"Resultado de {tool}:\n{result}"})

    # MAX_ROUNDS atingido — salva memória parcial e retorna
    log.warning(f"MAX_ROUNDS ({MAX_ROUNDS}) atingido. Salvando memória parcial.")
    partial_answer = f"Tarefa interrompida após {MAX_ROUNDS} rounds. Trabalho parcial realizado em {len(set(sources))} nota(s)."
    if root:
        try:
            actions_summary = _build_actions_summary(sources, messages)
            save_session_memory(root, question, partial_answer, list(set(sources)), actions_summary)
        except Exception as e:
            log.warning(f"Falha ao salvar memória parcial: {e}")
    return {"answer": partial_answer, "sources": list(set(sources))}


def _build_actions_summary(sources: list, messages: list) -> str:
    """Gera um resumo compacto das ações executadas a partir das fontes e mensagens."""
    if not sources:
        return "nenhuma ação executada"

    # Conta tipos de ação a partir das mensagens de resultado
    counts = {"movidas": 0, "criadas": 0, "editadas": 0, "deletadas": 0, "tags": 0}
    for msg in messages:
        if msg.get("role") == "user":
            c = msg.get("content", "")
            if "Resultado de move_note" in c:
                counts["movidas"] += 1
            elif "Resultado de create_note" in c or "Resultado de ensure_settings" in c:
                counts["criadas"] += 1
            elif "Resultado de edit_note" in c:
                counts["editadas"] += 1
            elif "Resultado de delete_note" in c:
                counts["deletadas"] += 1
            elif "Resultado de add_tags" in c:
                counts["tags"] += 1

    parts = []
    if counts["movidas"]:
        parts.append(f"{counts['movidas']} nota(s) movida(s)")
    if counts["criadas"]:
        parts.append(f"{counts['criadas']} nota(s) criada(s)")
    if counts["editadas"]:
        parts.append(f"{counts['editadas']} nota(s) editada(s)")
    if counts["deletadas"]:
        parts.append(f"{counts['deletadas']} nota(s) deletada(s)")
    if counts["tags"]:
        parts.append(f"{counts['tags']} tag(s) adicionada(s)")

    return ", ".join(parts) if parts else f"{len(set(sources))} nota(s) afetada(s)"


# ── Ações rápidas (mantidas para compatibilidade) ─────────────────────────────
def weekly_summary(root: Optional[str] = None) -> dict:
    return ask("Gere um resumo executivo semanal com: projetos em andamento e status, riscos e bloqueios, próximos passos prioritários, stakeholders que precisam de atenção.", root=root)


def market_insights(root: Optional[str] = None) -> dict:
    return ask("Com base nas análises e estudos registrados, identifique: principais tendências de mercado, oportunidades, riscos competitivos e gaps de conhecimento.", root=root)


def summarize_meeting(note_id: str, root: Optional[str] = None) -> dict:
    return ask(f"Leia a nota {note_id} e gere: resumo executivo, decisões tomadas, action items com responsável e prazo, pontos que precisam de follow-up.", root=root)


def vault_review(root: Optional[str] = None) -> dict:
    return ask(
        "Liste todas as notas do vault. Para cada nota que tiver wiki links no formato [[caminho/Nota]] sem alias, "
        "leia a nota e corrija para [[caminho/Nota|Nota]]. Também identifique correlações óbvias entre notas e adicione links onde pertinente. "
        "Reporte quais notas foram modificadas.",
        root=root,
    )
