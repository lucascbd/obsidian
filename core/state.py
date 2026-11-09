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
            CREATE TABLE IF NOT EXISTS events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      REAL NOT NULL,
                ts_iso  TEXT NOT NULL,
                vault   TEXT,
                actor   TEXT,
                kind    TEXT,
                source  TEXT,
                ref     TEXT,
                summary TEXT,
                detail  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts_iso);
            CREATE TABLE IF NOT EXISTS processed_items (
                source       TEXT NOT NULL,   -- 'outlook' | 'teams'
                item_id      TEXT NOT NULL,   -- id estável (InternetMessageID / chat msg id)
                item_date    TEXT,            -- data do email/mensagem
                processed_at TEXT,            -- quando foi processado
                note_path    TEXT,            -- nota gerada no vault
                status       TEXT,            -- 'ok' | 'error'
                PRIMARY KEY (source, item_id)
            );
            CREATE INDEX IF NOT EXISTS idx_processed_date ON processed_items (source, item_date);
            CREATE TABLE IF NOT EXISTS todos (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                vault      TEXT,
                title      TEXT NOT NULL,
                due        TEXT,            -- prazo (YYYY-MM-DD) ou vazio
                status     TEXT DEFAULT 'open',   -- 'open' | 'done'
                source     TEXT,            -- 'email' | 'teams' | 'manual' | ...
                ref        TEXT,            -- msg_id / origem
                note_path  TEXT,            -- nota relacionada no vault
                created_at TEXT,
                done_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_todos_status ON todos (vault, status);
            CREATE TABLE IF NOT EXISTS ingest_queue (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                source     TEXT NOT NULL,
                item_id    TEXT,
                payload    TEXT NOT NULL,   -- JSON do email/mensagem
                created_at TEXT,
                attempts   INTEGER DEFAULT 0
            );
            """
        )


# ── Registro de itens processados (dedup entre email/Teams) ──────────────────────

# Status considerados "resolvidos" (não reprocessar). 'error' fica de fora de
# propósito: itens marcados com erro transitório (ex.: timeout 524) são retomados.
_DONE_STATUSES = ("ok", "pending", "skip")


def is_processed(source: str, item_id: str) -> bool:
    """True se o item já foi resolvido (ok/pending/skip) — não deve ser reenfileirado."""
    if not item_id:
        return False
    with _conn() as c:
        row = c.execute(
            "SELECT status FROM processed_items WHERE source=? AND item_id=?", (source, item_id)
        ).fetchone()
    return row is not None and row["status"] in _DONE_STATUSES


# ── Fila de ingestão (processamento assíncrono pelo worker do agente) ────────────

def enqueue(source: str, item_id: str, payload: dict) -> None:
    import time, json as _json
    now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO ingest_queue (source, item_id, payload, created_at, attempts) VALUES (?,?,?,?,0)",
            (source, item_id, _json.dumps(payload, ensure_ascii=False), now_iso),
        )


def queue_peek():
    """Item mais antigo da fila, ou None. Retorna dict {id, source, item_id, payload(dict), attempts}."""
    import json as _json
    with _conn() as c:
        row = c.execute(
            "SELECT id, source, item_id, payload, attempts FROM ingest_queue ORDER BY id ASC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        payload = _json.loads(row["payload"])
    except Exception:
        payload = {}
    return {"id": row["id"], "source": row["source"], "item_id": row["item_id"],
            "payload": payload, "attempts": row["attempts"]}


def queue_delete(qid: int) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM ingest_queue WHERE id=?", (qid,))


def queue_bump(qid: int) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE ingest_queue SET attempts = attempts + 1 WHERE id=?", (qid,))


def queue_len() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM ingest_queue").fetchone()["n"]


# ── To-dos (extraídos de emails/reuniões + manuais) ─────────────────────────────

def add_todo(vault: str, title: str, due: str = "", source: str = "", ref: str = "",
             note_path: str = "") -> int:
    import time
    title = (title or "").strip()
    if not title:
        return 0
    now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with _lock, _conn() as c:
        # evita duplicar o mesmo título vindo da mesma origem
        if ref:
            ex = c.execute("SELECT id FROM todos WHERE vault=? AND ref=? AND title=?",
                           (vault, ref, title)).fetchone()
            if ex:
                return ex["id"]
        cur = c.execute(
            "INSERT INTO todos (vault, title, due, status, source, ref, note_path, created_at) "
            "VALUES (?,?,?,'open',?,?,?,?)",
            (vault, title[:300], (due or "")[:10], source, ref, note_path, now_iso),
        )
        return cur.lastrowid


def list_todos(vault: str, status: str = None, limit: int = 200) -> list:
    q = "SELECT id, title, due, status, source, ref, note_path, created_at, done_at FROM todos WHERE vault=?"
    params = [vault]
    if status:
        q += " AND status=?"
        params.append(status)
    q += " ORDER BY (due='') ASC, due ASC, id DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def set_todo_status(todo_id: int, status: str) -> None:
    import time
    done_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) if status == "done" else None
    with _lock, _conn() as c:
        c.execute("UPDATE todos SET status=?, done_at=? WHERE id=?", (status, done_at, todo_id))


def count_todos(vault: str, status: str = "open") -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM todos WHERE vault=? AND status=?",
                         (vault, status)).fetchone()["n"]


def mark_processed(source: str, item_id: str, item_date: str = "",
                   note_path: str = "", status: str = "ok") -> None:
    if not item_id:
        return
    import time
    now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO processed_items (source, item_id, item_date, processed_at, note_path, status) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(source, item_id) DO UPDATE SET processed_at=excluded.processed_at, "
            "note_path=excluded.note_path, status=excluded.status",
            (source, item_id, item_date, now_iso, note_path, status),
        )


def processed_ids(source: str, since: str = None) -> list:
    """IDs já processados de uma fonte. since (YYYY-MM-DD) limita por item_date."""
    done = "('ok','pending','skip')"
    if since:
        q = (f"SELECT item_id FROM processed_items WHERE source=? AND status IN {done} "
             "AND (item_date IS NULL OR substr(item_date,1,10) >= ?)")
        params = (source, since[:10])
    else:
        q = f"SELECT item_id FROM processed_items WHERE source=? AND status IN {done}"
        params = (source,)
    with _conn() as c:
        rows = c.execute(q, params).fetchall()
    return [r["item_id"] for r in rows]


# ── Registro de atividade (linha do tempo) ───────────────────────────────────────

def record_event(vault: str, actor: str, kind: str, summary: str,
                 source: str = "", ref: str = "", detail=None) -> None:
    """Grava um evento na linha do tempo. actor='agent'|'user'|'system'."""
    import time, json as _json
    now = time.time()
    ts_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    det = detail if isinstance(detail, str) else (_json.dumps(detail, ensure_ascii=False) if detail else "")
    try:
        with _lock, _conn() as c:
            c.execute(
                "INSERT INTO events (ts, ts_iso, vault, actor, kind, source, ref, summary, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (now, ts_iso, vault, actor, kind, source, ref, (summary or "")[:500], det[:4000]),
            )
    except Exception:
        pass  # log de atividade nunca deve derrubar uma ação


def query_events(vault: str = None, date_from: str = None, date_to: str = None,
                 actor: str = None, kind: str = None, limit: int = 500) -> list:
    """Retorna eventos ordenados por tempo. date_from/date_to no formato YYYY-MM-DD
    (inclusivos, comparados pela data do ts_iso)."""
    clauses, params = [], []
    if vault:
        clauses.append("vault = ?"); params.append(vault)
    if date_from:
        clauses.append("substr(ts_iso,1,10) >= ?"); params.append(date_from[:10])
    if date_to:
        clauses.append("substr(ts_iso,1,10) <= ?"); params.append(date_to[:10])
    if actor:
        clauses.append("actor = ?"); params.append(actor)
    if kind:
        clauses.append("kind = ?"); params.append(kind)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(limit, 2000)))
    with _conn() as c:
        rows = c.execute(
            f"SELECT ts_iso, actor, kind, source, ref, summary FROM events {where} "
            f"ORDER BY ts ASC LIMIT ?", params,
        ).fetchall()
    return [dict(r) for r in rows]


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
