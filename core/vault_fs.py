"""vault_fs.py — armazenamento do vault em disco.

Substitui o acesso ao CouchDB/LiveSync. Cada vault é um subdiretório de
VAULT_ROOT; cada nota é um arquivo .md cujo note_id é o caminho relativo ao
vault (ex.: 'projetos/x/nota.md'). Escrita atômica (tmp + os.replace).
"""

import os
import shutil
import hashlib
import logging
from pathlib import Path

from .config import VAULT_ROOT, IGNORE_DIRS, is_meta

log = logging.getLogger(__name__)


# ── Resolução de caminhos (com proteção contra path traversal) ──────────────────

def vault_dir(vault: str) -> Path:
    return VAULT_ROOT / vault


def _resolve(vault: str, note_id: str) -> Path:
    base = vault_dir(vault).resolve()
    note_id = note_id.strip().lstrip("/")
    if not note_id.endswith(".md"):
        note_id += ".md"
    target = (base / note_id).resolve()
    if base not in target.parents and target != base:
        raise ValueError(f"Caminho fora do vault: {note_id}")
    if not target.exists():
        # note_ids são guardados em minúsculas, mas o disco diferencia maiúsculas:
        # acha o arquivo/pasta existente ignorando o caso (evita "não encontrada" e
        # a criação de uma cópia duplicada em minúsculas ao escrever).
        ci = _match_case_insensitive(base, target.relative_to(base).parts)
        if ci is not None:
            return ci
    return target


def _match_case_insensitive(base: Path, parts) -> "Path | None":
    """Caminha a partir de base casando cada componente sem diferenciar maiúsculas.
    Retorna o maior prefixo existente + o resto (para escrever numa pasta existente
    com outro caso), ou None se nada casar além de base."""
    cur, matched = base, 0
    for i, part in enumerate(parts):
        nxt = cur / part
        if not nxt.exists():
            low = part.lower()
            try:
                hit = next((c for c in cur.iterdir() if c.name.lower() == low), None)
            except (FileNotFoundError, NotADirectoryError):
                hit = None
            if hit is None:
                break
            nxt = hit
        cur, matched = nxt, i + 1
    if matched == 0:
        return None
    return cur.joinpath(*parts[matched:]) if matched < len(parts) else cur


def note_id_of(vault: str, path: Path) -> str:
    """Caminho relativo (note_id) de um arquivo dentro do vault, em lowercase."""
    return path.resolve().relative_to(vault_dir(vault).resolve()).as_posix().lower()


# ── Descoberta ────────────────────────────────────────────────────────────────

def get_vaults() -> list:
    """Cada subdiretório de VAULT_ROOT é um vault."""
    if not VAULT_ROOT.exists():
        return []
    return sorted(
        p.name for p in VAULT_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def _iter_md(vault: str):
    base = vault_dir(vault)
    if not base.exists():
        return
    for p in base.rglob("*.md"):
        parts = set(p.relative_to(base).parts)
        if parts & IGNORE_DIRS:
            continue
        yield p


def list_notes(vault: str, include_meta: bool = False) -> list:
    """note_ids de todas as notas ativas do vault (exclui 00-meta por padrão)."""
    out = []
    for p in _iter_md(vault):
        nid = note_id_of(vault, p)
        if not include_meta and is_meta(nid):
            continue
        out.append(nid)
    return sorted(out)


def iter_notes(vault: str):
    """Gera (note_id, Path) para cada arquivo .md indexável (exclui 00-meta)."""
    for p in _iter_md(vault):
        nid = note_id_of(vault, p)
        if is_meta(nid):
            continue
        yield nid, p


def exists(vault: str, note_id: str) -> bool:
    try:
        return _resolve(vault, note_id).is_file()
    except ValueError:
        return False


# ── Leitura ───────────────────────────────────────────────────────────────────

def read_note(vault: str, note_id: str) -> str:
    return _resolve(vault, note_id).read_text(encoding="utf-8")


def read_path(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Escrita ───────────────────────────────────────────────────────────────────

def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def write_note(vault: str, note_id: str, content: str) -> str:
    """Cria ou sobrescreve uma nota. Retorna o note_id normalizado."""
    target = _resolve(vault, note_id)
    _atomic_write(target, content)
    nid = note_id_of(vault, target)
    log.info(f"[{vault}] write {nid}")
    return nid


# create_note é alias de write_note (disco não distingue criar/editar)
create_note = write_note


def move_note(vault: str, source_id: str, dest_id: str) -> str:
    src = _resolve(vault, source_id)
    dst = _resolve(vault, dest_id)
    if not src.is_file():
        raise FileNotFoundError(source_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    log.info(f"[{vault}] move {source_id} → {dest_id}")
    return note_id_of(vault, dst)


def delete_note(vault: str, note_id: str) -> bool:
    target = _resolve(vault, note_id)
    if target.is_file():
        target.unlink()
        log.info(f"[{vault}] delete {note_id}")
        return True
    return False
