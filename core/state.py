"""state.py — estado de sincronização em SQLite.

Controla o que já foi indexado (sha256 por arquivo), quais chunk ids pertencem a
cada nota (para upsert/delete limpo no Chroma) e o grafo de wiki links
(substitui o doc _local/link-graph do CouchDB).

Thread-safe: cada operação abre sua própria conexão (check_same_thread=False não
é suficiente quando várias threads do watcher escrevem). WAL ligado para
concorrência leitura/escrita.
"""

import sqlite3
import threading
from contextlib import contextmanager

from .config import STATE_DB, DATA_DIR

_lock = threading.Lock()


@contextmanager
def _conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(STATE_DB, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with _lock, _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
                vault      TEXT NOT NULL,
                path       TEXT NOT NULL,
                sha256     TEXT NOT NULL,
                mtime      REAL,
                indexed_at REAL,
                PRIMARY KEY (vault, path)
            );
            CREATE TABLE IF NOT EXISTS chunks (
                vault    TEXT NOT NULL,
                path     TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                PRIMARY KEY (vault, path, chunk_id)
            );
            CREATE TABLE IF NOT EXISTS links (
                vault TEXT NOT NULL,
                src   TEXT NOT NULL,
                dst   TEXT NOT NULL,
                PRIMARY KEY (vault, src, dst)
            );
            CREATE INDEX IF NOT EXISTS idx_links_dst ON links (vault, dst);
            """
        )


# ── Controle de alterações ──────────────────────────────────────────────────────

def needs_reindex(vault: str, path: str, sha: str) -> bool:
    """True se o arquivo é novo ou mudou desde a última indexação."""
    with _conn() as c:
        row = c.execute(
            "SELECT sha256 FROM notes WHERE vault=? AND path=?", (vault, path)
        ).fetchone()
    return row is None or row["sha256"] != sha


def mark_indexed(vault: str, path: str, sha: str, mtime: float) -> None:
    import time
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO notes (vault, path, sha256, mtime, indexed_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(vault, path) DO UPDATE SET sha256=excluded.sha256, "
            "mtime=excluded.mtime, indexed_at=excluded.indexed_at",
            (vault, path, sha, mtime, time.time()),
        )


def forget_note(vault: str, path: str) -> None:
    """Remove todo o estado de uma nota (ao deletar/mover)."""
    with _lock, _conn() as c:
        c.execute("DELETE FROM notes  WHERE vault=? AND path=?", (vault, path))
        c.execute("DELETE FROM chunks WHERE vault=? AND path=?", (vault, path))
        c.execute("DELETE FROM links  WHERE vault=? AND src=?",  (vault, path))


def known_paths(vault: str) -> set:
    """Paths já registrados para um vault (para reconciliar com o disco)."""
    with _conn() as c:
        rows = c.execute("SELECT path FROM notes WHERE vault=?", (vault,)).fetchall()
    return {r["path"] for r in rows}


# ── Chunks (ligação nota → ids no Chroma) ────────────────────────────────────────

def record_chunks(vault: str, path: str, chunk_ids: list) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM chunks WHERE vault=? AND path=?", (vault, path))
        c.executemany(
            "INSERT OR IGNORE INTO chunks (vault, path, chunk_id) VALUES (?,?,?)",
            [(vault, path, cid) for cid in chunk_ids],
        )


def chunk_ids(vault: str, path: str) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT chunk_id FROM chunks WHERE vault=? AND path=?", (vault, path)
        ).fetchall()
    return [r["chunk_id"] for r in rows]


# ── Grafo de links ───────────────────────────────────────────────────────────────

def set_links(vault: str, src: str, dsts: list) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM links WHERE vault=? AND src=?", (vault, src))
        c.executemany(
            "INSERT OR IGNORE INTO links (vault, src, dst) VALUES (?,?,?)",
            [(vault, src, d) for d in dsts],
        )


def outgoing(vault: str, note_id: str) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT dst FROM links WHERE vault=? AND src=?", (vault, note_id)
        ).fetchall()
    return [r["dst"] for r in rows]


def incoming(vault: str, note_id: str) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT src FROM links WHERE vault=? AND dst=?", (vault, note_id)
        ).fetchall()
    return [r["src"] for r in rows]


def link_graph(vault: str) -> dict:
    """Mapa completo src → [dst] (compatível com o formato antigo do CouchDB)."""
    with _conn() as c:
        rows = c.execute("SELECT src, dst FROM links WHERE vault=?", (vault,)).fetchall()
    graph: dict[str, list] = {}
    for r in rows:
        graph.setdefault(r["src"], []).append(r["dst"])
    return graph
