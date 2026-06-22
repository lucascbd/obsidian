"""
agent.py — Agente com tool calling: lê e escreve no vault via CouchDB/ChromaDB.
"""

import os
import re
import io
import json
import logging
import unicodedata
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
EMBEDDING_MODEL  = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
LINK_GRAPH_DOC   = "_local/link-graph"

COUCHDB_URL  = os.environ.get("COUCHDB_URL", "http://couchdb:5984")
COUCHDB_USER = os.environ.get("COUCHDB_USER", "")
COUCHDB_PASS = os.environ.get("COUCHDB_PASSWORD", "")
COUCHDB_DB   = os.environ.get("COUCHDB_DB", "obsidian-vault")
COUCHDB_AUTH = (COUCHDB_USER, COUCHDB_PASS)

import re as _re
_INVALID_COL = _re.compile(r"[^a-zA-Z0-9_-]")


def _col_name(vault: str) -> str:
    """Uma coleção ChromaDB por vault (mesmo esquema do watcher)."""
    col = _INVALID_COL.sub("_", vault)[:63]
    return col or "vault"

MAX_ROUNDS = 500

_embed_model  = None
_chroma       = None
_stop_event   = __import__("threading").Event()


def stop_agent():
    """Sinaliza o loop de tool calling para parar."""
    _stop_event.set()


def reset_stop():
    """Limpa o sinal de stop antes de iniciar nova operação."""
    _stop_event.clear()

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
        log.info(f"Carregando modelo de embeddings: {EMBEDDING_MODEL}")
        _embed_model = TextEmbedding(EMBEDDING_MODEL)
    return _embed_model


def get_chroma():
    global _chroma
    if _chroma is None:
        _chroma = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    return _chroma


# ── CouchDB ───────────────────────────────────────────────────────────────────
def _couch(method: str, path: str, db: str = None, **kwargs):
    _db = db or COUCHDB_DB
    url = f"{COUCHDB_URL}/{_db}/{requests.utils.quote(path, safe='')}"
    r = getattr(requests, method)(url, auth=COUCHDB_AUTH, timeout=15, **kwargs)
    r.raise_for_status()
    return r.json()


def _read_note_content(doc: dict, db: str = None) -> str:
    parts = []
    for leaf_id in doc.get("children", []):
        try:
            leaf = _couch("get", leaf_id, db=db)
            parts.append(leaf.get("data", ""))
        except Exception:
            pass
    return "".join(parts)


def _write_note_content(doc: dict, new_content: str, db: str = None) -> bool:
    import time

    children = doc.get("children", [])
    if not children:
        return False

    leaves = []
    for leaf_id in children:
        try:
            leaves.append(_couch("get", leaf_id, db=db))
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
        _couch("put", leaf["_id"], db=db, json=leaf)

    fresh = _couch("get", doc["_id"], db=db)
    fresh["size"]  = len(new_content.encode("utf-8"))
    fresh["mtime"] = int(time.time() * 1000)
    _couch("put", fresh["_id"], db=db, json=fresh)
    return True


def get_vaults() -> list:
    """Lista os bancos de dados CouchDB disponíveis (cada um é um vault)."""
    system_dbs = {"_users", "_replicator", "_global_changes"}
    try:
        r = requests.get(f"{COUCHDB_URL}/_all_dbs", auth=COUCHDB_AUTH, timeout=10)
        r.raise_for_status()
        vaults = [v for v in r.json() if v not in system_dbs]
        log.info(f"get_vaults: {vaults}")
        return vaults
    except Exception as e:
        log.error(f"Erro ao listar vaults: {e}")
        return []


def get_root_folders() -> list:
    """Alias de get_vaults() para compatibilidade."""
    return get_vaults()


# ── Sistema 00-meta/config/ ───────────────────────────────────────────────────
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


def load_settings(vault: str, db: str = None) -> dict:
    """Lê as notas de config do vault em 00-meta/config/. Retorna dict com profile, load e mem."""
    settings = {"profile": "", "load": "", "mem": ""}
    mapping = {
        "profile": "00-meta/config/profile.md",
        "load":    "00-meta/config/load.md",
        "mem":     "00-meta/config/agent-mem.md",
    }
    for key, note_id in mapping.items():
        try:
            doc = _couch("get", note_id, db=db)
            if not doc.get("deleted"):
                content = _read_note_content(doc, db=db)
                settings[key] = content
        except Exception:
            pass
    return settings


def save_session_memory(vault: str, question: str, answer: str, sources: list,
                        actions_summary: str, db: str = None) -> None:
    """Faz append em 00-meta/config/agent-mem.md com o resumo da sessão."""
    import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M") + " UTC"
    note_id = "00-meta/config/agent-mem.md"

    notes_bullets = "\n".join(f"- {s}" for s in sources) if sources else "- (nenhuma nota afetada)"
    new_entry = (
        f"\n## Sessão {now}\n"
        f"**Vault:** {vault}\n"
        f"**Tarefa:** {question}\n"
        f"**Ações:** {actions_summary}\n"
        f"**Notas afetadas:**\n{notes_bullets}\n"
        f"---\n"
    )

    try:
        doc = _couch("get", note_id, db=db)
        if doc.get("deleted"):
            raise Exception("nota deletada")
        existing = _read_note_content(doc, db=db)
        _write_note_content(doc, existing + new_entry, db=db)
    except Exception:
        tool_create_note(note_id, f"# Memória de Sessões\n{new_entry}", db=db)


def tool_ensure_settings(vault: str, db: str = None) -> str:
    """Cria 00-meta/config/profile.md e load.md com conteúdo padrão se não existirem."""
    created = []
    for note_id, default_content in [
        ("00-meta/config/profile.md", DEFAULT_PROFILE_MD),
        ("00-meta/config/load.md",    DEFAULT_LOAD_MD),
    ]:
        try:
            doc = _couch("get", note_id, db=db)
            if not doc.get("deleted"):
                continue
        except Exception:
            pass
        result = tool_create_note(note_id, default_content, db=db)
        created.append(f"{note_id}: {result}")

    if not created:
        return "Arquivos de settings já existem em '00-meta/config/'."
    return "Settings criados:\n" + "\n".join(created)


# ── Tools disponíveis para o agente ──────────────────────────────────────────
def purge_vault(keep_prefix: Optional[str] = None, db: str = None) -> dict:
    """Marca todas as notas ativas como deleted=true no CouchDB (formato LiveSync).
    keep_prefix: se fornecido, preserva notas que começam com esse prefixo."""
    import time
    _db = db or COUCHDB_DB
    r = requests.get(
        f"{COUCHDB_URL}/{_db}/_all_docs",
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
            _couch("put", nid, json=doc, db=_db)
            purged.append(nid)
            log.info(f"Purged: {nid}")
        except Exception as e:
            log.error(f"Erro ao purgar {nid}: {e}")
    return {
        "answer": f"{len(purged)} nota(s) removida(s) do banco.\n" + "\n".join(f"- {n}" for n in purged),
        "sources": purged,
        "skipped": skipped,
    }


def purge_orphan_leaves(db: str = None) -> dict:
    """Remove leaf docs (h:) cujo pai está deletado ou não existe."""
    _db = db or COUCHDB_DB
    r = requests.get(
        f"{COUCHDB_URL}/{_db}/_all_docs",
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
                f"{COUCHDB_URL}/{_db}/{requests.utils.quote(nid, safe='')}",
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


def repair_vault(db: str = None) -> dict:
    """Corrige size mismatch em documentos do CouchDB."""
    _db = db or COUCHDB_DB
    r = requests.get(
        f"{COUCHDB_URL}/{_db}/_all_docs",
        params={"include_docs": True},
        auth=COUCHDB_AUTH, timeout=30,
    )
    r.raise_for_status()
    fixed = []
    for row in r.json().get("rows", []):
        doc = row.get("doc", {})
        if doc.get("deleted") or doc.get("type") != "plain" or doc["_id"].startswith("_") or doc["_id"].startswith("h:"):
            continue
        content = _read_note_content(doc, db=_db)
        real_size = len(content.encode("utf-8"))
        if doc.get("size", -1) != real_size:
            import time
            doc["size"] = real_size
            doc["mtime"] = int(time.time() * 1000)
            try:
                _couch("put", doc["_id"], json=doc, db=_db)
                fixed.append(f"{doc['_id']} ({doc.get('size', '?')} → {real_size})")
                log.info(f"Reparado: {doc['_id']}")
            except Exception as e:
                log.error(f"Erro ao reparar {doc['_id']}: {e}")
    return {
        "answer": f"{len(fixed)} documento(s) reparado(s).\n" + "\n".join(fixed) if fixed else "Nenhum documento com size incorreto encontrado.",
        "sources": fixed,
    }

_INTERNAL_DOCS = {"obsydian_livesync_version", "obsidian_livesync_version"}


def _chunk_text(text: str, size: int = 400, overlap: int = 50) -> list:
    words = text.split()
    if not words:
        return []
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i:i + size]))
        i += size - overlap
    return chunks


def _parse_frontmatter(content: str) -> dict:
    meta: dict = {}
    if not content.startswith("---"):
        return meta
    end = content.find("\n---", 3)
    if end == -1:
        return meta
    fm = content[3:end]
    for key, pat in [("title", r"^title:\s*(.+)$"), ("date", r"^date:\s*(.+)$"), ("source", r"^source:\s*(.+)$")]:
        m = re.search(pat, fm, re.MULTILINE)
        if m:
            meta[key] = m.group(1).strip().strip("\"'")
    m = re.search(r"^tags:(.*?)(?=\n\S|\Z)", fm, re.MULTILINE | re.DOTALL)
    if m:
        block = m.group(1)
        tags = re.findall(r"[\-\*]\s*(\S+)", block)
        if not tags:
            tags = [t.strip().strip("\"'") for t in block.strip().strip("[]").split(",") if t.strip()]
        if tags:
            meta["tags"] = ",".join(tags[:10])
    return meta


def reindex_vault(vault: Optional[str] = None) -> dict:
    """Reconstrói do zero a coleção ChromaDB de um vault: deleta, re-embedda e re-indexa todas as notas."""
    vaults_to_run = [vault] if vault else get_vaults()
    results = []

    for v in vaults_to_run:
        col_nm = _col_name(v)
        chroma = get_chroma()
        model  = get_embed_model()

        # Deleta coleção antiga
        try:
            chroma.delete_collection(col_nm)
            log.info(f"[reindex] Coleção '{col_nm}' deletada")
        except Exception:
            pass

        col = chroma.get_or_create_collection(col_nm)
        log.info(f"[reindex] Coleção '{col_nm}' criada")

        skip, limit, indexed, errors = 0, 100, 0, 0
        while True:
            try:
                r = requests.get(
                    f"{COUCHDB_URL}/{v}/_all_docs",
                    params={"include_docs": True, "limit": limit, "skip": skip},
                    auth=COUCHDB_AUTH, timeout=30,
                )
                rows = r.json().get("rows", [])
            except Exception as e:
                log.error(f"[reindex] _all_docs erro: {e}")
                break
            if not rows:
                break

            for row in rows:
                doc = row.get("doc", {})
                nid = doc.get("_id", "")
                if (not nid or nid.startswith("_") or nid.startswith("h:")
                        or doc.get("deleted") or _is_settings_note(nid)):
                    continue
                try:
                    content = _read_note_content(doc, db=v)
                    if not content or not content.strip():
                        continue
                    fm     = _parse_frontmatter(content)
                    chunks = _chunk_text(content)
                    if not chunks:
                        continue
                    embeddings = [e.tolist() for e in model.embed(chunks)]
                    base_meta  = {"note_id": nid, "vault": v, **fm}
                    col.add(
                        documents  = chunks,
                        embeddings = embeddings,
                        ids        = [f"{v}__{nid}__c{i}" for i in range(len(chunks))],
                        metadatas  = [{**base_meta, "chunk": i} for i in range(len(chunks))],
                    )
                    indexed += 1
                except Exception as e:
                    log.error(f"[reindex] Erro em {nid}: {e}")
                    errors += 1

            skip += limit

        msg = f"[{v}] {indexed} nota(s) reindexadas" + (f", {errors} erro(s)" if errors else "")
        log.info(msg)
        results.append(msg)

    return {
        "answer": "✅ Reindexação concluída:\n" + "\n".join(f"- {r}" for r in results),
        "sources": [],
    }
    """Retorna True se a nota pertence à pasta 00-meta ou é doc interno do LiveSync."""
    if note_id in _INTERNAL_DOCS:
        return True
    parts = note_id.split("/")
    return "00-meta" in parts


def tool_search_vault(query: str, collections: Optional[list] = None, db: str = None,
                      tags: Optional[str] = None, date_from: Optional[str] = None) -> str:
    """Busca semântica no vault (uma coleção por vault). Suporta filtro por tags e data."""
    vault  = db or COUCHDB_DB
    col_nm = _col_name(vault)
    model  = get_embed_model()
    chroma = get_chroma()

    try:
        col = chroma.get_collection(col_nm)
    except Exception:
        return "Vault ainda não indexado pelo watcher."

    embedding = list(model.embed([query]))[0].tolist()

    # Filtros de metadata opcionais
    where: dict = {}
    if tags:
        where["tags"] = {"$contains": tags}
    if date_from:
        where["date"] = {"$gte": date_from}

    try:
        r = col.query(
            query_embeddings=[embedding],
            n_results=12,
            where=where if where else None,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        return f"Erro na busca: {e}"

    seen, results = set(), []
    for doc, meta, dist in zip(r["documents"][0], r["metadatas"][0], r["distances"][0]):
        nid = meta.get("note_id", "")
        if not nid or _is_settings_note(nid) or nid in seen:
            continue
        seen.add(nid)
        results.append({
            "note_id": nid,
            "score":   round(1 - dist, 3),
            "excerpt": doc[:400],
            "title":   meta.get("title", ""),
            "tags":    meta.get("tags", ""),
            "date":    meta.get("date", ""),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    if not results:
        return "Nenhuma nota encontrada."
    return json.dumps(results[:8], ensure_ascii=False)


def _get_link_graph(db: str = None) -> dict:
    """Lê o grafo de links do CouchDB (_local não replica via LiveSync)."""
    _db = db or COUCHDB_DB
    try:
        r = requests.get(f"{COUCHDB_URL}/{_db}/{LINK_GRAPH_DOC}", auth=COUCHDB_AUTH, timeout=10)
        if r.status_code == 200:
            return r.json().get("links", {})
    except Exception:
        pass
    return {}


def tool_get_neighbors(note_id: str, db: str = None) -> str:
    """Retorna notas diretamente conectadas via wiki links (grafo de conhecimento).
    outgoing = notas que esta nota menciona; incoming = notas que mencionam esta."""
    graph    = _get_link_graph(db=db)
    outgoing = graph.get(note_id, [])
    incoming = [nid for nid, links in graph.items() if note_id in links]
    return json.dumps({
        "note_id":       note_id,
        "outgoing_links": outgoing,
        "incoming_links": incoming,
        "degree":        len(set(outgoing + incoming)),
    }, ensure_ascii=False)


def tool_graph_search(start_note_id: str, depth: int = 2, db: str = None) -> str:
    """BFS no grafo de links a partir de uma nota — mapeia a rede de conhecimento conectada.
    depth=1 vizinhos diretos, depth=2 vizinhos de vizinhos, etc."""
    graph   = _get_link_graph(db=db)
    visited: dict[str, dict] = {}
    queue   = [(start_note_id, 0)]

    while queue:
        node, d = queue.pop(0)
        if node in visited or d > depth:
            continue
        outgoing = graph.get(node, [])
        incoming = [nid for nid, links in graph.items() if node in links]
        visited[node] = {"depth": d, "outgoing": outgoing, "incoming": incoming}
        for neighbor in set(outgoing + incoming):
            if neighbor not in visited:
                queue.append((neighbor, d + 1))

    return json.dumps({
        "start":        start_note_id,
        "depth":        depth,
        "nodes_found":  len(visited),
        "network":      visited,
    }, ensure_ascii=False)


def tool_read_note(note_id: str, db: str = None) -> str:
    """Lê o conteúdo completo de uma nota do CouchDB."""
    try:
        doc = _couch("get", note_id, db=db)
        content = _read_note_content(doc, db=db)
        return content if content else "(nota vazia)"
    except Exception as e:
        return f"Erro ao ler nota '{note_id}': {e}"


def tool_edit_note(note_id: str, content: str, db: str = None) -> str:
    """Edita o conteúdo de uma nota existente no CouchDB."""
    try:
        doc = _couch("get", note_id, db=db)
        if doc.get("deleted"):
            return f"Nota '{note_id}' está deletada."
        ok = _write_note_content(doc, content, db=db)
        return f"Nota '{note_id}' atualizada com sucesso." if ok else "Falha ao salvar."
    except Exception as e:
        return f"Erro ao editar nota '{note_id}': {e}"


def tool_list_notes(db: str = None) -> str:
    """Lista todas as notas ativas (não deletadas) do vault. Exclui notas de 00-meta."""
    _db = db or COUCHDB_DB
    try:
        r = requests.get(
            f"{COUCHDB_URL}/{_db}/_all_docs",
            params={"include_docs": True},
            auth=COUCHDB_AUTH, timeout=30,
        )
        r.raise_for_status()
        ids = [
            row["id"] for row in r.json().get("rows", [])
            if not row["id"].startswith("_")
            and not row["id"].startswith("h:")
            and not row.get("doc", {}).get("deleted")
            and not _is_settings_note(row["id"])
        ]
        return json.dumps(ids, ensure_ascii=False)
    except Exception as e:
        return f"Erro ao listar notas: {e}"


def tool_create_note(path: str, content: str, db: str = None) -> str:
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
        _couch("put", leaf_id, db=db, json=leaf)
        _couch("put", note_id, db=db, json=doc)
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
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.com/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    session = requests.Session()
    r = session.get(url, timeout=30, headers=headers, allow_redirects=True)
    if r.status_code == 403:
        raise Exception(
            f"403 Forbidden — o site bloqueou o acesso automático. "
            f"Copie o texto da página e importe como arquivo .txt."
        )
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

{load_rules_section}

REGRAS GERAIS (aplicar somente se não houver REGRAS DO VAULT acima):
- Estrutura: sempre 3 níveis de hierarquia (ex: categoria/tema/nome.md)
- Slug em lowercase com hifens, sem acentos
- Frontmatter obrigatório: title, date ({today}), source, tags (3-5)
- Seções com ## e ###, conteúdo em português, conciso e navegável
- Wiki links com alias: [[caminho/nota|Nome Visível]]

NOTAS EXISTENTES NO VAULT:
{note_ids}

Responda APENAS com um JSON válido (sem texto antes ou depois):
{{"path": "nivel1/nivel2/nivel3/nome.md", "content": "conteúdo completo da nota em markdown", "summary": "1-2 frases descrevendo o que foi criado"}}"""


def _find_related_for_new_note(content: str, exclude_path: str, db: str = None, threshold: float = 0.72) -> list:
    """Busca notas semanticamente relacionadas para auto-linking no ingest."""
    vault = db or COUCHDB_DB
    try:
        col = get_chroma().get_collection(_col_name(vault))
        embedding = list(get_embed_model().embed([content[:1000]]))[0].tolist()
        r = col.query(query_embeddings=[embedding], n_results=10, include=["metadatas", "distances"])
        related, seen = [], {exclude_path.lower()}
        for meta, dist in zip(r["metadatas"][0], r["distances"][0]):
            nid = meta.get("note_id", "")
            score = round(1 - dist, 3)
            if nid and nid not in seen and score >= threshold and not _is_settings_note(nid):
                related.append({"note_id": nid, "title": meta.get("title") or nid.split("/")[-1].replace(".md", ""), "score": score})
                seen.add(nid)
        return related[:5]
    except Exception:
        return []


def _graph_enrich_search(args: dict, db: str = None) -> str:
    """Chama tool_search_vault e enriquece cada resultado com notas conectadas no grafo."""
    raw = tool_search_vault(db=db, **args)
    try:
        results = json.loads(raw)
        graph = _get_link_graph(db=db)
        for item in results:
            nid = item.get("note_id", "")
            if not nid:
                continue
            out = graph.get(nid, [])
            inc = [k for k, v in graph.items() if nid in v]
            connected = list(dict.fromkeys(out + inc))[:6]
            if connected:
                item["connected_notes"] = connected
        return json.dumps(results, ensure_ascii=False)
    except Exception:
        return raw


def ingest(raw_text: str, source_name: str, vault: Optional[str] = None, db: str = None) -> dict:
    """Converte texto bruto em nota Obsidian e salva no vault."""
    import datetime
    db = db or vault  # vault name == CouchDB database name
    note_ids = tool_list_notes(db=db)
    today = datetime.date.today().isoformat()

    # Carrega regras do vault — têm prioridade sobre as regras gerais
    load_rules_section = ""
    if vault:
        settings = load_settings(vault, db=db)
        if settings.get("load"):
            load_rules_section = (
                f"⚠️ REGRAS DO VAULT (PRIORIDADE MÁXIMA — seguir à risca, ignorar qualquer exemplo genérico abaixo):\n"
                f"{settings['load']}"
            )

    vault_ctx = f"\nVAULT: {vault}" if vault else ""
    prompt = (
        INGEST_PROMPT
        .replace("{load_rules_section}", load_rules_section)
        .replace("{note_ids}", note_ids[:3000])
        .replace("{today}", today)
    ) + vault_ctx
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

        # Auto-link: busca notas relacionadas e adiciona seção antes de salvar
        related = _find_related_for_new_note(content, path, db=db)
        if related and "## Notas Relacionadas" not in content:
            links = "\n".join(
                f"- [[{r['note_id']}|{r['title']}]]  (similaridade: {r['score']})"
                for r in related
            )
            content += f"\n\n## Notas Relacionadas\n{links}"

        result = tool_create_note(path, content, db=db)
        log.info(f"Ingest: {result}")

        return {
            "answer":  f"{summary}\n\nNota criada: `{path}`\n\n{result}",
            "sources": [path],
            "path":    path,
        }

    return {"answer": "Falha ao processar o conteúdo após 3 tentativas.", "sources": []}


def ingest_file(filename: str, content: bytes, vault: Optional[str] = None, db: str = None) -> dict:
    raw_text = _extract_text_from_file(filename, content)
    return ingest(raw_text, source_name=filename, vault=vault, db=db)


def ingest_url(url: str, vault: Optional[str] = None, db: str = None) -> dict:
    try:
        raw_text = _extract_text_from_url(url)
    except Exception as e:
        return {"answer": f"Erro ao buscar URL: {e}", "sources": []}
    return ingest(raw_text, source_name=url, vault=vault, db=db)


def ingest_zip(content: bytes, vault: Optional[str] = None, db: str = None) -> dict:
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
                    result     = ingest_file(basename, file_bytes, vault=vault, db=db)
                    created.append(result.get("path", basename))
                    log.info(f"ZIP ingest OK: {result.get('path')}")
                except Exception as e:
                    errors.append(f"{name}: {e}")
                    log.error(f"ZIP ingest erro {name}: {e}")
    except zipfile.BadZipFile:
        return {"answer": "Arquivo ZIP inválido ou corrompido.", "sources": []}

    lines = [f"**{len(created)} notas criadas** de {len(names)} arquivos no ZIP"]
    if vault:
        lines.append(f"Vault: `{vault}`")
    lines.append("")
    lines.extend(f"- `{p}`" for p in created)
    if errors:
        lines.append(f"\n**{len(errors)} erros:**")
        lines.extend(f"- {e}" for e in errors)

    return {"answer": "\n".join(lines), "sources": created}


# ── Tool calling loop ─────────────────────────────────────────────────────────
def tool_move_note(source_id: str, dest_id: str, db: str = None) -> str:
    """Move/renomeia uma nota: copia conteúdo para o novo ID e marca o original como deleted."""
    import time, hashlib
    source_id = source_id.lower().strip()
    dest_id   = dest_id.lower().strip()
    if source_id == dest_id:
        return f"Nota já está em '{dest_id}' — nenhuma ação necessária."
    if _is_settings_note(source_id):
        return f"Nota '{source_id}' é interna/protegida e não pode ser movida."
    try:
        src_doc = _couch("get", source_id, db=db)
        if src_doc.get("deleted"):
            return f"Nota '{source_id}' já está deletada."
        content = _read_note_content(src_doc, db=db)

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
        _couch("put", leaf_id, db=db, json=leaf)
        _couch("put", dest_id, db=db, json=doc)

        src_doc["deleted"] = True
        src_doc["mtime"]   = int(time.time() * 1000)
        _couch("put", source_id, db=db, json=src_doc)

        log.info(f"move_note: {source_id} → {dest_id}")
        return f"Nota movida: '{source_id}' → '{dest_id}'"
    except Exception as e:
        return f"Erro ao mover nota: {e}"


def tool_delete_note(note_id: str, db: str = None) -> str:
    """Marca uma nota como deleted=true no CouchDB (formato LiveSync)."""
    import time
    try:
        doc = _couch("get", note_id, db=db)
        if doc.get("deleted"):
            return f"Nota '{note_id}' já estava deletada."
        doc["deleted"] = True
        doc["mtime"]   = int(time.time() * 1000)
        _couch("put", note_id, db=db, json=doc)
        log.info(f"delete_note: {note_id}")
        return f"Nota '{note_id}' deletada."
    except Exception as e:
        return f"Erro ao deletar nota '{note_id}': {e}"


def tool_add_tags(note_id: str, tags: list, db: str = None) -> str:
    """Adiciona tags ao frontmatter YAML de uma nota. Cria o bloco --- se não existir. Não duplica tags já presentes."""
    try:
        doc = _couch("get", note_id, db=db)
        content = _read_note_content(doc, db=db)

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
        ok = _write_note_content(doc, new_content, db=db)
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
            "description": "Busca semântica nas notas do vault via embeddings. Use para encontrar notas relevantes. Suporta filtro por tags e data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":     {"type": "string", "description": "Texto para buscar semanticamente"},
                    "tags":      {"type": "string", "description": "Filtrar por tag específica (ex: 'haleon', 'mercado')"},
                    "date_from": {"type": "string", "description": "Filtrar notas a partir desta data (YYYY-MM-DD)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_neighbors",
            "description": "Retorna notas diretamente conectadas a uma nota via wiki links. Use para navegar no grafo de conhecimento.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "ID da nota para verificar conexões"},
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "graph_search",
            "description": "BFS no grafo de links a partir de uma nota. Mapeia a rede de conhecimento conectada. Use para encontrar clusters temáticos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_note_id": {"type": "string", "description": "Nota inicial para o BFS"},
                    "depth":         {"type": "integer", "description": "Profundidade do BFS (1=vizinhos diretos, 2=vizinhos de vizinhos). Padrão: 2"},
                },
                "required": ["start_note_id"],
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
            "description": "Cria os arquivos de configuração do agente (profile.md e load.md) em 00-meta/config/ se não existirem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vault": {"type": "string", "description": "Nome do vault (banco de dados CouchDB)"},
                },
                "required": ["vault"],
            },
        },
    },
]

TOOL_FNS = {
    "search_vault":    lambda args: tool_search_vault(**args),
    "get_neighbors":   lambda args: tool_get_neighbors(**args),
    "graph_search":    lambda args: tool_graph_search(**args),
    "read_note":       lambda args: tool_read_note(**args),
    "list_notes":      lambda args: tool_list_notes(),
    "edit_note":       lambda args: tool_edit_note(**args),
    "create_note":     lambda args: tool_create_note(**args),
    "move_note":       lambda args: tool_move_note(**args),
    "delete_note":     lambda args: tool_delete_note(**args),
    "add_tags":        lambda args: tool_add_tags(**args),
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

{"tool": "search_vault", "args": {"query": "...", "tags": "opcional", "date_from": "opcional YYYY-MM-DD"}}
{"tool": "get_neighbors", "args": {"note_id": "caminho/nota.md"}}
{"tool": "graph_search", "args": {"start_note_id": "caminho/nota.md", "depth": 2}}
{"tool": "read_note", "args": {"note_id": "caminho/nota.md"}}
{"tool": "list_notes", "args": {}}
{"tool": "edit_note", "args": {"note_id": "caminho/nota.md", "content": "conteúdo markdown completo"}}
{"tool": "create_note", "args": {"path": "pasta/subpasta/nota.md", "content": "conteúdo markdown completo"}}
{"tool": "move_note", "args": {"source_id": "caminho/atual.md", "dest_id": "caminho/novo.md"}}
{"tool": "delete_note", "args": {"note_id": "caminho/nota.md"}}
{"tool": "add_tags", "args": {"note_id": "caminho/nota.md", "tags": ["tag1", "tag2"]}}
{"tool": "ensure_settings", "args": {"vault": "nome-do-vault"}}
{"tool": "done", "args": {"answer": "resumo completo do que foi feito"}}

USO EFICIENTE DAS FERRAMENTAS — OBRIGATÓRIO:

❌ NUNCA faça list_notes + read_note em loop para analisar o vault. Isso é O(n) e lento.
✅ USE search_vault com queries temáticas para descobrir clusters de conteúdo.

QUANDO ANALISAR ESTRUTURA, CORRELAÇÕES OU ORGANIZAÇÃO:
1. Faça várias chamadas search_vault com queries temáticas (ex: "reunião projeto", "análise mercado", "empresa parceiro", "decisão estratégica", etc.)
2. Use graph_search nas notas mais conectadas para mapear clusters
3. Use list_notes APENAS para obter a lista de caminhos/pastas existentes — nunca para ler conteúdo
4. Use read_note APENAS quando precisar do conteúdo COMPLETO de uma nota específica já identificada

QUANDO USAR CADA FERRAMENTA:
- search_vault → descoberta semântica, correlações, "quais notas falam sobre X?"
- graph_search → mapear redes de conhecimento, clusters conectados por wiki links
- get_neighbors → explorar conexões de uma nota específica já conhecida
- list_notes → ver estrutura de pastas e caminhos (não conteúdo)
- read_note → ler conteúdo de nota JÁ identificada como relevante

GRAFO DE CONHECIMENTO:
- get_neighbors: vizinhos diretos (1 hop) de uma nota — rápido para explorar conexões imediatas
- graph_search: BFS depth=2 para mapear clusters temáticos — use quando precisar entender contexto amplo
- search_vault retorna connected_notes de cada resultado (notas conectadas no grafo)

REGRAS DE EXECUÇÃO:
- Execute a tarefa COMPLETA até o fim. Se há 50 notas para processar, processe as 50.
- Não pare no meio para "listar pendências" — execute as pendências.
- Se uma subtarefa falhar, continue com as próximas sem parar.
- Ao reorganizar pastas: use move_note (não criar + deletar manualmente).
- Ao padronizar tags: use add_tags (mais seguro que edit_note para só adicionar tags).
- Ao deletar duplicatas: confirme que o conteúdo foi movido antes de usar delete_note.

RESPEITO À INTENÇÃO DO USUÁRIO — CRÍTICO:
- Se o usuário disser "só analise", "não mova ainda", "mostre primeiro", "me pergunte antes" → chame done com a PROPOSTA em texto. NUNCA execute move_note/edit_note/delete_note/create_note sem confirmação explícita.
- Se o usuário disser "faça", "execute", "aplique", "pode mover" → execute sem pedir confirmação.
- Dúvida sobre a intenção? Trate como "só análise" e apresente a proposta no done.

RASTREAMENTO DE ESTADO — CRÍTICO:
- Antes de mover uma nota, verifique mentalmente se ela já foi movida nesta sessão.
- NUNCA tente mover uma nota para o mesmo caminho onde ela já está.
- Se um move_note retornar erro indicando que a nota não existe no source, ela já foi movida — pule.
- Mantenha uma lista mental de "já processados" e não processe duas vezes.

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


def ask(question: str, collections: Optional[list] = None, vault: Optional[str] = None,
        on_progress=None) -> dict:
    """Loop de tool calling via prompt até o agente chamar 'done'.
    vault = nome do banco CouchDB a usar (se None, usa COUCHDB_DB do env)."""

    db = vault  # vault name == CouchDB database name

    # Carrega settings do vault e injeta no system prompt
    settings_ctx = ""
    if vault:
        settings = load_settings(vault, db=db)
        parts = []
        if settings.get("profile"):
            parts.append(f"## PERFIL DO AGENTE (do vault)\n{settings['profile']}")
        if settings.get("mem"):
            parts.append(f"## MEMÓRIA DE SESSÕES ANTERIORES\n{settings['mem'][-3000:]}")
        if parts:
            settings_ctx = "\n\n" + "\n\n".join(parts)

    vault_ctx = (
        f"\n\nVAULT ATIVO: '{vault}'\n"
        f"- Você está trabalhando no vault '{vault}' (banco de dados CouchDB separado)\n"
        f"- As notas de configuração estão em '00-meta/config/'\n"
        f"- list_notes e search_vault já operam sobre este vault\n"
    ) if vault else ""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + settings_ctx + vault_ctx},
        {"role": "user",   "content": question},
    ]
    sources = []

    # Ferramentas com db vinculado ao vault escolhido
    scoped_fns = {
        "search_vault":    lambda args: _graph_enrich_search(args, db=db),
        "get_neighbors":   lambda args: tool_get_neighbors(db=db, **args),
        "graph_search":    lambda args: tool_graph_search(db=db, **args),
        "read_note":       lambda args: tool_read_note(db=db, **args),
        "list_notes":      lambda _: tool_list_notes(db=db),
        "edit_note":       lambda args: tool_edit_note(db=db, **args),
        "create_note":     lambda args: tool_create_note(db=db, **args),
        "move_note":       lambda args: tool_move_note(db=db, **args),
        "delete_note":     lambda args: tool_delete_note(db=db, **args),
        "add_tags":        lambda args: tool_add_tags(db=db, **args),
        "ensure_settings": lambda args: tool_ensure_settings(args.get("vault") or vault or "", db=db),
    }

    reset_stop()
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

        if _stop_event.is_set():
            log.info("Agente parado pelo usuário")
            return {"answer": "⛔ Operação interrompida pelo usuário.", "sources": list(set(sources))}

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
            if vault:
                try:
                    actions_summary = _build_actions_summary(sources, messages)
                    save_session_memory(vault, question, answer, list(set(sources)), actions_summary, db=db)
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
            sources.append(args.get("note_id") or args.get("path") or args.get("vault", ""))
        elif tool == "move_note":
            sources.append(args.get("dest_id", ""))
        elif tool == "delete_note":
            sources.append(args.get("note_id", ""))

        messages.append({"role": "user", "content": f"Resultado de {tool}:\n{result}"})

    # MAX_ROUNDS atingido — salva memória parcial e retorna
    log.warning(f"MAX_ROUNDS ({MAX_ROUNDS}) atingido. Salvando memória parcial.")
    partial_answer = f"Tarefa interrompida após {MAX_ROUNDS} rounds. Trabalho parcial realizado em {len(set(sources))} nota(s)."
    if vault:
        try:
            actions_summary = _build_actions_summary(sources, messages)
            save_session_memory(vault, question, partial_answer, list(set(sources)), actions_summary, db=db)
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


# ── Normalização de tags (operação mecânica, sem LLM) ────────────────────────
def _normalize_tag(tag: str) -> str:
    """Lowercase, sem acentos, espaços e underscores → hífens, remove chars especiais."""
    tag = unicodedata.normalize("NFD", tag)
    tag = "".join(c for c in tag if unicodedata.category(c) != "Mn")
    tag = tag.lower().strip()
    tag = re.sub(r"[\s_]+", "-", tag)
    tag = re.sub(r"[^a-z0-9\-]", "", tag)
    tag = re.sub(r"-+", "-", tag).strip("-")
    return tag


def _fix_codeblock_frontmatter(content: str) -> str:
    """Converte ```yaml\\n...\\n``` no início da nota para frontmatter --- correto."""
    m = re.match(r"^```(?:yaml)?\n(.*?)\n```\s*\n?", content, re.DOTALL)
    if m:
        yaml_body = m.group(1)
        rest = content[m.end():]
        return f"---\n{yaml_body}\n---\n{rest.lstrip()}"
    return content


def _fix_tags_in_frontmatter(fm_body: str) -> str:
    """Normaliza o bloco de tags dentro do frontmatter (lista YAML ou array inline)."""
    def _replace(m):
        block = m.group(0)

        # Array inline: tags: ["crítica pessoal", "Saúde"] ou tags: [tag1, tag2]
        inline = re.match(r'^tags:\s*\[(.+)\]\s*$', block.strip(), re.DOTALL)
        if inline:
            raw_tags = re.findall(r'"([^"]+)"|\'([^\']+)\'|([^,\[\]\s]+)', inline.group(1))
            tags = [_normalize_tag(next(p for p in t if p)) for t in raw_tags]
            tags = [t for t in tags if t]
            return "tags:\n" + "\n".join(f"  - {t}" for t in tags)

        # Lista YAML: tags:\n  - valor
        lines = block.split("\n")
        out = []
        for line in lines:
            item = re.match(r"^(\s*[-*]\s*)(.+)$", line)
            if item:
                raw = item.group(2).strip().strip("\"'")
                norm = _normalize_tag(raw)
                if norm:
                    out.append(f"{item.group(1)}{norm}")
            else:
                out.append(line)
        return "\n".join(out)

    return re.sub(r"^tags:.*?(?=\n\w|\Z)", _replace, fm_body, flags=re.MULTILINE | re.DOTALL)


def normalize_tags(vault: Optional[str] = None) -> dict:
    """Lê todas as notas do vault, normaliza tags no frontmatter (sem LLM).
    Se vault=None, processa todos os vaults disponíveis."""
    vaults = [vault] if vault else get_vaults()
    if not vaults:
        return {"answer": "Nenhum vault encontrado no CouchDB.", "sources": []}

    all_updated, all_errors = [], []

    for _db in vaults:
        try:
            r = requests.get(
                f"{COUCHDB_URL}/{_db}/_all_docs",
                params={"include_docs": True},
                auth=COUCHDB_AUTH, timeout=30,
            )
            r.raise_for_status()
        except Exception as e:
            all_errors.append(f"[{_db}] erro ao listar: {e}")
            continue

        rows = r.json().get("rows", [])
        for row in rows:
            doc = row.get("doc", {})
            nid = row["id"]
            if (nid.startswith("_") or nid.startswith("h:")
                    or doc.get("deleted") or _is_settings_note(nid)):
                continue

            try:
                content = _read_note_content(doc, db=_db)
            except Exception as e:
                all_errors.append(f"[{_db}] {nid}: {e}")
                continue

            if not content:
                continue

            # 1. Converte ```yaml ... ``` para frontmatter --- correto
            new_content = _fix_codeblock_frontmatter(content)

            # 2. Normaliza tags dentro do frontmatter ---
            if new_content.startswith("---"):
                end = new_content.find("\n---", 3)
                if end != -1 and "tags:" in new_content[:end]:
                    fm_body    = new_content[3:end]
                    fixed_fm   = _fix_tags_in_frontmatter(fm_body)
                    new_content = f"---{fixed_fm}\n---{new_content[end + 4:]}"
            elif "tags:" not in new_content:
                continue  # sem tags, pula

            if new_content == content:
                continue

            try:
                ok = _write_note_content(doc, new_content, db=_db)
                if ok:
                    all_updated.append(f"{_db}/{nid}")
                    log.info(f"normalize_tags: [{_db}] {nid}")
                else:
                    all_errors.append(f"[{_db}] {nid}: falha ao salvar")
            except Exception as e:
                all_errors.append(f"[{_db}] {nid}: {e}")

    lines = [f"**{len(all_updated)} nota(s) atualizadas**, {len(all_errors)} erro(s)."]
    if all_updated:
        lines.append("\n**Atualizadas:**")
        lines.extend(f"- `{n}`" for n in all_updated)
    if all_errors:
        lines.append("\n**Erros:**")
        lines.extend(f"- {e}" for e in all_errors)

    return {"answer": "\n".join(lines), "sources": all_updated}


# ── Ações rápidas ────────────────────────────────────────────────────────────
def weekly_summary(vault: Optional[str] = None) -> dict:
    return ask("Gere um resumo executivo semanal com: projetos em andamento e status, riscos e bloqueios, próximos passos prioritários, stakeholders que precisam de atenção.", vault=vault)


def market_insights(vault: Optional[str] = None) -> dict:
    return ask("Com base nas análises e estudos registrados, identifique: principais tendências de mercado, oportunidades, riscos competitivos e gaps de conhecimento.", vault=vault)


def summarize_meeting(note_id: str, vault: Optional[str] = None) -> dict:
    return ask(f"Leia a nota {note_id} e gere: resumo executivo, decisões tomadas, action items com responsável e prazo, pontos que precisam de follow-up.", vault=vault)


def vault_review(vault: Optional[str] = None) -> dict:
    """Revisão do vault via ChromaDB (zero LLM). Uma coleção por vault.
    Descobre correlações vetoriais e adiciona wiki links nas notas."""
    db                   = vault
    SIMILARITY_THRESHOLD = 0.78
    MAX_LINKS_PER_NOTE   = 5

    note_ids_raw = tool_list_notes(db=db)
    try:
        all_note_ids = set(json.loads(note_ids_raw))
    except Exception:
        return {"answer": "Erro ao listar notas.", "sources": []}

    if not all_note_ids:
        return {"answer": "Vault vazio.", "sources": []}

    vault_name = vault or COUCHDB_DB
    chroma     = get_chroma()

    try:
        col  = chroma.get_collection(_col_name(vault_name))
        data = col.get(include=["embeddings", "metadatas"])
    except Exception:
        return {"answer": "ChromaDB: vault não indexado — execute o watcher primeiro.", "sources": []}

    # Embedding representativo por nota (primeiro chunk)
    note_embeddings: dict[str, list] = {}
    for emb, meta in zip(data["embeddings"], data["metadatas"]):
        nid = meta.get("note_id", "")
        if nid and nid in all_note_ids and not _is_settings_note(nid) and nid not in note_embeddings:
            note_embeddings[nid] = emb

    if not note_embeddings:
        return {"answer": "Nenhuma nota indexada no ChromaDB.", "sources": []}

    log.info(f"vault_review: {len(note_embeddings)} notas, buscando correlações ≥ {SIMILARITY_THRESHOLD}")

    # Correlações via queries vetoriais (sem LLM)
    note_to_similar: dict[str, list] = {}
    for note_id, embedding in note_embeddings.items():
        try:
            r = col.query(
                query_embeddings=[embedding],
                n_results=MAX_LINKS_PER_NOTE + 2,
                include=["metadatas", "distances"],
            )
            seen, similars = {note_id}, []
            for meta, dist in zip(r["metadatas"][0], r["distances"][0]):
                sim_id = meta.get("note_id", "")
                score  = round(1 - dist, 3)
                if sim_id and sim_id not in seen and score >= SIMILARITY_THRESHOLD \
                        and not _is_settings_note(sim_id) and sim_id in all_note_ids:
                    similars.append((sim_id, score))
                    seen.add(sim_id)
            if similars:
                note_to_similar[note_id] = sorted(similars, key=lambda x: -x[1])[:MAX_LINKS_PER_NOTE]
        except Exception:
            pass

    log.info(f"vault_review: {len(note_to_similar)} notas com correlações")

    # Lê apenas notas com links faltando (k << n)
    edited = []
    for note_id, similars in note_to_similar.items():
        content = tool_read_note(note_id, db=db)
        if content.startswith("Erro") or content == "(nota vazia)":
            continue

        # Corrige wiki links sem alias via regex
        new_content = re.sub(
            r'\[\[([^|\]\n]+)\]\]',
            lambda m: f'[[{m.group(1)}|{m.group(1).split("/")[-1].replace("-", " ").title()}]]',
            content,
        )

        missing = [(sid, sc) for sid, sc in similars if f'[[{sid}' not in new_content]
        if not missing and new_content == content:
            continue

        if missing:
            links_md = "\n".join(
                f"- [[{sid}|{sid.split('/')[-1].replace('-', ' ').title()}]]"
                for sid, _ in missing
            )
            if "## Notas Relacionadas" in new_content:
                new_content = re.sub(
                    r'(## Notas Relacionadas\n)',
                    lambda m: m.group(1) + links_md + "\n",
                    new_content, count=1,
                )
            else:
                new_content = new_content.rstrip() + f"\n\n## Notas Relacionadas\n{links_md}\n"

        tool_edit_note(note_id, new_content, db=db)
        edited.append(note_id)
        log.info(f"vault_review: {note_id} +{len(missing)} link(s)")

    if not edited:
        return {
            "answer": f"Vault revisado: {len(note_embeddings)} notas analisadas. Nenhuma atualização necessária.",
            "sources": [],
        }

    return {
        "answer": (
            f"Vault revisado via ChromaDB — {len(note_embeddings)} notas, zero chamadas LLM.\n"
            f"{len(edited)} nota(s) atualizada(s):\n\n"
            + "\n".join(f"- `{n}`" for n in edited)
        ),
        "sources": edited,
    }
