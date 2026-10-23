"""indexer.py — indexação de notas no ChromaDB a partir do disco.

Uma coleção por vault. Chunking por seção + entidades no frontmatter. O estado
(sha, chunk ids, links) fica no SQLite, permitindo upsert/delete limpos e pular
arquivos não alterados. Usado pelo watcher (eventos de filesystem) e pelo
agente (reindex manual).
"""

import logging
import threading

import chromadb
from fastembed import TextEmbedding

from . import state, vault_fs
from .config import CHROMA_HOST, CHROMA_PORT, EMBEDDING_MODEL, col_name, is_meta
from .text import parse_frontmatter, chunk_by_sections, extract_wiki_links

log = logging.getLogger(__name__)

_model = None
_chroma = None
_model_lock = threading.Lock()


def get_model() -> TextEmbedding:
    global _model
    if _model is None:
        log.info(f"Carregando modelo de embeddings: {EMBEDDING_MODEL}")
        _model = TextEmbedding(EMBEDDING_MODEL)
    return _model


def get_chroma() -> chromadb.HttpClient:
    global _chroma
    if _chroma is None:
        _chroma = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    return _chroma


def get_collection(vault: str):
    return get_chroma().get_or_create_collection(col_name(vault))


def _chunk_id(vault: str, note_id: str, i: int) -> str:
    return f"{vault}__{note_id}__c{i}"


def index_note(vault: str, note_id: str, content: str, col=None, force: bool = False) -> int:
    """Indexa uma nota (chunk → embed → upsert). Retorna o nº de chunks.
    Pula se o sha não mudou (a menos que force). Mantém SQLite (sha, chunks, links)."""
    if is_meta(note_id):
        return 0
    if not content or not content.strip():
        return 0

    sha = vault_fs.sha256_of(content)
    if not force and not state.needs_reindex(vault, note_id, sha):
        return 0

    col = col or get_collection(vault)
    chunks = chunk_by_sections(content)
    if not chunks:
        return 0

    # Remove chunks antigos desta nota antes de reinserir (upsert limpo).
    old = state.chunk_ids(vault, note_id)
    if old:
        try:
            col.delete(ids=old)
        except Exception:
            pass
    try:
        col.delete(where={"note_id": note_id})
    except Exception:
        pass

    model = get_model()
    with _model_lock:
        embeddings = [e.tolist() for e in model.embed(chunks)]

    fm = parse_frontmatter(content)
    base_meta = {"note_id": note_id, "vault": vault, **fm}
    ids = [_chunk_id(vault, note_id, i) for i in range(len(chunks))]
    col.add(
        documents=chunks,
        embeddings=embeddings,
        ids=ids,
        metadatas=[{**base_meta, "chunk": i} for i in range(len(chunks))],
    )

    links = extract_wiki_links(content)
    state.set_links(vault, note_id, links)
    state.record_chunks(vault, note_id, ids)

    import os
    try:
        mtime = os.path.getmtime(vault_fs._resolve(vault, note_id))
    except Exception:
        mtime = 0.0
    state.mark_indexed(vault, note_id, sha, mtime)

    log.info(f"[{vault}] indexado {note_id} — {len(chunks)} chunk(s), {len(links)} link(s)")
    return len(chunks)


def remove_note(vault: str, note_id: str, col=None) -> None:
    """Remove uma nota do índice e do estado (ao deletar/mover)."""
    col = col or get_collection(vault)
    old = state.chunk_ids(vault, note_id)
    if old:
        try:
            col.delete(ids=old)
        except Exception:
            pass
    try:
        col.delete(where={"note_id": note_id})
    except Exception:
        pass
    state.forget_note(vault, note_id)
    log.info(f"[{vault}] removido {note_id}")


def reconcile(vault: str, col=None, force: bool = False) -> dict:
    """Varre o disco e sincroniza o índice: indexa novos/alterados e remove órfãos.
    Usada no startup do watcher e no reindex manual (force=True recria tudo)."""
    col = col or get_collection(vault)
    seen, indexed = set(), 0

    for note_id, path in vault_fs.iter_notes(vault):
        seen.add(note_id)
        try:
            content = vault_fs.read_path(path)
            indexed += 1 if index_note(vault, note_id, content, col=col, force=force) else 0
        except Exception as e:
            log.error(f"[{vault}] erro ao indexar {note_id}: {e}")

    # Remove do índice notas que sumiram do disco.
    orphans = state.known_paths(vault) - seen
    for note_id in orphans:
        remove_note(vault, note_id, col=col)

    log.info(f"[{vault}] reconcile: {indexed} (re)indexada(s), {len(orphans)} órfã(s) removida(s)")
    return {"indexed": indexed, "removed": len(orphans), "total": len(seen)}
