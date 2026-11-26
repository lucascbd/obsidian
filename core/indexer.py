"""indexer.py — indexação de notas no ChromaDB a partir do disco.

Uma coleção por vault. Chunking por seção + entidades no frontmatter. O estado
(sha, chunk ids, links) fica no SQLite, permitindo upsert/delete limpos e pular
arquivos não alterados. Usado pelo watcher (eventos de filesystem) e pelo
agente (reindex manual).
"""

import os
import logging

import chromadb
from chromadb.config import Settings

from . import state, vault_fs, embedder
from .config import CHROMA_HOST, CHROMA_PORT, EMBEDDING_MODEL, col_name, is_meta
from .text import parse_frontmatter, chunk_by_sections, extract_wiki_links, strip_related_section

log = logging.getLogger(__name__)

_chroma = None

# Coleções criadas antes de gravarmos o modelo nos metadados foram geradas com este.
LEGACY_MODEL = os.environ.get("EMBED_LEGACY_MODEL", "intfloat/multilingual-e5-large")


def get_chroma() -> chromadb.HttpClient:
    global _chroma
    if _chroma is None:
        # Telemetria desligada: o chromadb 0.5.20 quebra com o posthog >= 6
        # ("capture() takes 1 positional argument but 3 were given").
        _chroma = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT,
                                      settings=Settings(anonymized_telemetry=False))
    return _chroma


def get_collection(vault: str):
    """Coleção do vault. Ao criar, grava qual modelo gera os vetores (ver ensure_model)."""
    ch, name = get_chroma(), col_name(vault)
    try:
        return ch.get_collection(name)
    except Exception:
        pass
    try:
        return ch.create_collection(name, metadata={"embed_model": EMBEDDING_MODEL})
    except Exception as create_err:
        try:
            return ch.get_collection(name)  # outro processo criou no meio-tempo
        except Exception:
            raise create_err from None


def ensure_model(vault: str) -> bool:
    """Garante que a coleção foi gerada pelo modelo configurado. Se o EMBEDDING_MODEL
    mudou (a dimensão dos vetores muda junto), recria a coleção vazia e esquece o
    estado de indexação do vault — o reconcile seguinte reindexa tudo.
    Retorna True se recriou. Chamado pelo watcher antes do reconcile."""
    ch, name = get_chroma(), col_name(vault)
    col = get_collection(vault)
    meta = dict(col.metadata or {})
    stored = meta.get("embed_model") or LEGACY_MODEL
    if stored == EMBEDDING_MODEL:
        if not meta.get("embed_model"):
            try:
                col.modify(metadata={**meta, "embed_model": EMBEDDING_MODEL})
            except Exception as e:
                log.warning(f"[{vault}] não gravou embed_model nos metadados: {e}")
        return False
    log.warning(f"[{vault}] modelo de embeddings mudou ({stored} → {EMBEDDING_MODEL}): "
                f"recriando a coleção e reindexando tudo")
    ch.delete_collection(name)
    state.forget_vault_index(vault)
    ch.create_collection(name, metadata={"embed_model": EMBEDDING_MODEL})
    return True


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
    chunks = chunk_by_sections(strip_related_section(content))
    if not chunks:
        return 0

    # Embeda ANTES de apagar os chunks antigos: se falhar (ex.: agent fora do ar),
    # a nota continua com o índice anterior e o próximo rescan tenta de novo.
    embeddings = embedder.embed(chunks)

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
        except embedder.EmbedError as e:
            # Agent fora do ar: não insiste nota por nota; o próximo rescan retoma.
            log.warning(f"[{vault}] reconcile interrompido (embeddings indisponíveis): {e}")
            return {"indexed": indexed, "removed": 0, "total": len(seen), "aborted": True}
        except Exception as e:
            log.error(f"[{vault}] erro ao indexar {note_id}: {e}")

    # Remove do índice notas que sumiram do disco.
    orphans = state.known_paths(vault) - seen
    for note_id in orphans:
        remove_note(vault, note_id, col=col)

    log.info(f"[{vault}] reconcile: {indexed} (re)indexada(s), {len(orphans)} órfã(s) removida(s)")
    return {"indexed": indexed, "removed": len(orphans), "total": len(seen)}
