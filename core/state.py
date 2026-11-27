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
                priority   TEXT DEFAULT 'media',  -- 'alta' | 'media' | 'baixa'
                link       TEXT,            -- link p/ a origem (ex.: abrir email no Outlook)
                created_at TEXT,
                done_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_todos_status ON todos (vault, status);
            CREATE TABLE IF NOT EXISTS drafts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                vault      TEXT,
                source     TEXT,            -- 'email' | 'teams'
                ref        TEXT,            -- msg_id da origem
                reply_to   TEXT,            -- destinatário sugerido (remetente original)
                subject    TEXT,
                body       TEXT,            -- rascunho (texto)
                status     TEXT DEFAULT 'pending',  -- 'pending' | 'sent' | 'dismissed'
                note_path  TEXT,
                priority   TEXT DEFAULT 'media',
                from_email TEXT,            -- endereço do remetente original
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts (vault, status);
            CREATE TABLE IF NOT EXISTS emails (
                vault         TEXT NOT NULL,
                msg_id        TEXT NOT NULL,   -- InternetMessageID (ou EntryID)
                entry_id      TEXT,            -- EntryID do Outlook (o collector abre por ele)
                subject       TEXT,
                sender        TEXT,
                from_email    TEXT,
                email_date    TEXT,
                note_path     TEXT,
                web_link      TEXT,
                draft_status  TEXT DEFAULT '', -- '' | waiting | generating | ready | error
                draft_error   TEXT,
                draft_requested_at TEXT,
                created_at    TEXT,
                PRIMARY KEY (vault, msg_id)
            );
            CREATE INDEX IF NOT EXISTS idx_emails_date ON emails (vault, email_date);
            CREATE TABLE IF NOT EXISTS kv (
                vault TEXT NOT NULL,
                key   TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (vault, key)
            );
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
        # Migração: adiciona priority em bancos antigos (ignora se já existe)
        try:
            c.execute("ALTER TABLE todos ADD COLUMN priority TEXT DEFAULT 'media'")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE todos ADD COLUMN link TEXT")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE drafts ADD COLUMN from_email TEXT")
        except Exception:
            pass
        # Uma vez: as prioridades dos to-dos eram automáticas; agora quem define é o
        # usuário, na tela. Zera as antigas.
        if not c.execute("SELECT 1 FROM kv WHERE vault='*' AND key='todo_priority_reset_v1'").fetchone():
            c.execute("UPDATE todos SET priority=''")
            c.execute("INSERT INTO kv (vault, key, value) VALUES ('*','todo_priority_reset_v1','1')")


# ── Registro de itens processados (dedup entre email/Teams) ──────────────────────

# Status considerados "resolvidos" (não reprocessar). 'error' fica de fora de
# propósito: itens marcados com erro transitório (ex.: timeout 524) são retomados.
_DONE_STATUSES = ("ok", "pending", "skip", "failed")


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
             note_path: str = "", priority: str = "", link: str = "") -> int:
    import time
    title = (title or "").strip()
    if not title:
        return 0
    if priority not in ("alta", "media", "baixa"):
        priority = ""          # sem prioridade: quem define é o usuário, na tela
    now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with _lock, _conn() as c:
        # evita duplicar o mesmo título vindo da mesma origem
        if ref:
            ex = c.execute("SELECT id FROM todos WHERE vault=? AND ref=? AND title=?",
                           (vault, ref, title)).fetchone()
            if ex:
                return ex["id"]
        cur = c.execute(
            "INSERT INTO todos (vault, title, due, status, source, ref, note_path, priority, link, created_at) "
            "VALUES (?,?,?,'open',?,?,?,?,?,?)",
            (vault, title[:300], (due or "")[:10], source, ref, note_path, priority, link, now_iso),
        )
        return cur.lastrowid


TODO_SORTS = {
    # data de inclusão: mais recentes primeiro
    "created":  "created_at DESC, id DESC",
    # prazo: mais próximo primeiro; sem prazo no fim
    "due":      "(COALESCE(due,'')='') ASC, due ASC, created_at DESC",
    # prioridade: alta → média → baixa → sem prioridade; depois prazo e inclusão
    "priority": ("CASE priority WHEN 'alta' THEN 0 WHEN 'media' THEN 1 WHEN 'baixa' THEN 2 ELSE 3 END, "
                 "(COALESCE(due,'')='') ASC, due ASC, created_at DESC"),
}


def list_todos(vault: str, status: str = None, limit: int = 200, sort: str = "created") -> list:
    q = ("SELECT id, title, due, status, source, ref, note_path, priority, link, created_at, done_at "
         "FROM todos WHERE vault=?")
    params = [vault]
    if status:
        q += " AND status=?"
        params.append(status)
    order = TODO_SORTS.get(sort, TODO_SORTS["created"])
    if status == "done" and sort == "created":
        order = "done_at DESC, id DESC"
    q += f" ORDER BY {order} LIMIT ?"
    params.append(limit)
    with _conn() as c:
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def set_todo_priority(todo_id: int, priority: str) -> None:
    if priority not in ("alta", "media", "baixa"):
        priority = ""
    with _lock, _conn() as c:
        c.execute("UPDATE todos SET priority=? WHERE id=?", (priority, todo_id))


def set_todo_status(todo_id: int, status: str) -> None:
    import time
    done_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) if status == "done" else None
    with _lock, _conn() as c:
        c.execute("UPDATE todos SET status=?, done_at=? WHERE id=?", (status, done_at, todo_id))


def count_todos(vault: str, status: str = "open") -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM todos WHERE vault=? AND status=?",
                         (vault, status)).fetchone()["n"]


# ── Rascunhos de resposta (email/Teams) ─────────────────────────────────────────

def has_draft(vault: str, ref: str) -> bool:
    if not ref:
        return False
    with _conn() as c:
        row = c.execute("SELECT 1 FROM drafts WHERE vault=? AND ref=? AND status!='dismissed'",
                        (vault, ref)).fetchone()
    return row is not None


def add_draft(vault: str, source: str, ref: str, subject: str, body: str,
              reply_to: str = "", note_path: str = "", priority: str = "media",
              from_email: str = "") -> int:
    import time
    if ref and has_draft(vault, ref):
        return 0
    now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO drafts (vault, source, ref, reply_to, subject, body, status, note_path, priority, "
            "from_email, created_at) VALUES (?,?,?,?,?,?,'pending',?,?,?,?)",
            (vault, source, ref, reply_to, (subject or "")[:300], body or "", note_path, priority,
             (from_email or "").lower(), now_iso),
        )
        return cur.lastrowid


def dismiss_drafts_matching(vault: str, patterns: list) -> int:
    """Descarta rascunhos pendentes cujo remetente (nome ou endereço) contenha algum
    trecho da lista 'nunca gerar rascunho'. Retorna quantos descartou."""
    pats = [p.strip().lower() for p in (patterns or []) if p and p.strip()]
    if not pats:
        return 0
    with _lock, _conn() as c:
        rows = c.execute("SELECT id, reply_to, from_email FROM drafts WHERE vault=? AND status='pending'",
                         (vault,)).fetchall()
        ids = [r["id"] for r in rows
               if any(p in f"{r['reply_to'] or ''} {r['from_email'] or ''}".lower() for p in pats)]
        for i in ids:
            c.execute("UPDATE drafts SET status='dismissed' WHERE id=?", (i,))
    return len(ids)


def list_drafts(vault: str, status: str = "pending", limit: int = 100) -> list:
    q = ("SELECT id, source, ref, reply_to, subject, body, status, note_path, priority, from_email, "
         "created_at FROM drafts WHERE vault=?")
    params = [vault]
    if status:
        q += " AND status=?"
        params.append(status)
    q += (" ORDER BY CASE priority WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END, id DESC LIMIT ?")
    params.append(limit)
    with _conn() as c:
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def set_draft_status(draft_id: int, status: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE drafts SET status=? WHERE id=?", (status, draft_id))


def count_drafts(vault: str, status: str = "pending") -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM drafts WHERE vault=? AND status=?",
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
    done = "('ok','pending','skip','failed')"
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


# ── Emails (lista do tile Email; o CORPO nunca é gravado) ─────────────────────────

def upsert_email(vault: str, msg_id: str, subject: str = "", sender: str = "", from_email: str = "",
                 email_date: str = "", note_path: str = "", web_link: str = "") -> None:
    import time
    if not msg_id:
        return
    wl = web_link or ""
    entry_id = wl.split(":", 1)[1] if wl.startswith(("obsmail:", "outlook:")) else ""
    now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO emails (vault, msg_id, entry_id, subject, sender, from_email, email_date, "
            "note_path, web_link, created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(vault, msg_id) DO UPDATE SET subject=excluded.subject, sender=excluded.sender, "
            "from_email=COALESCE(NULLIF(excluded.from_email,''), emails.from_email), "
            "email_date=excluded.email_date, note_path=COALESCE(NULLIF(excluded.note_path,''), emails.note_path), "
            "web_link=COALESCE(NULLIF(excluded.web_link,''), emails.web_link), "
            "entry_id=COALESCE(NULLIF(excluded.entry_id,''), emails.entry_id)",
            (vault, msg_id, entry_id, (subject or "")[:300], (sender or "")[:200], (from_email or "").lower(),
             email_date or "", note_path or "", web_link or "", now_iso))


def get_email(vault: str, msg_id: str) -> dict:
    with _conn() as c:
        r = c.execute("SELECT * FROM emails WHERE vault=? AND msg_id=?", (vault, msg_id)).fetchone()
    return dict(r) if r else {}


def list_emails(vault: str, limit: int = 60) -> list:
    """Emails mais recentes, com o rascunho (se houver) já anexado."""
    with _conn() as c:
        rows = c.execute(
            "SELECT e.*, d.id AS draft_id, d.body AS draft_body, d.subject AS draft_subject "
            "FROM emails e LEFT JOIN drafts d ON d.id = ("
            "  SELECT id FROM drafts WHERE vault=e.vault AND ref=e.msg_id AND status='pending' "
            "  ORDER BY id DESC LIMIT 1) "
            "WHERE e.vault=? ORDER BY e.email_date DESC, e.created_at DESC LIMIT ?",
            (vault, limit)).fetchall()
    return [dict(r) for r in rows]


def set_email_draft_status(vault: str, msg_id: str, status: str, error: str = "") -> None:
    import time
    now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with _lock, _conn() as c:
        if status == "waiting":
            c.execute("UPDATE emails SET draft_status=?, draft_error='', draft_requested_at=? "
                      "WHERE vault=? AND msg_id=?", (status, now_iso, vault, msg_id))
        else:
            c.execute("UPDATE emails SET draft_status=?, draft_error=? WHERE vault=? AND msg_id=?",
                      (status, error or "", vault, msg_id))


def pending_email_requests(limit: int = 10) -> list:
    """Pedidos de rascunho esperando o collector trazer o corpo do email do Outlook."""
    with _conn() as c:
        rows = c.execute("SELECT vault, msg_id, entry_id, subject FROM emails "
                         "WHERE draft_status='waiting' ORDER BY draft_requested_at LIMIT ?",
                         (limit,)).fetchall()
    return [dict(r) for r in rows]


def discard_email_drafts(vault: str, msg_id: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE drafts SET status='dismissed' WHERE vault=? AND ref=? AND status='pending'",
                  (vault, msg_id))
        c.execute("UPDATE emails SET draft_status='', draft_error='' WHERE vault=? AND msg_id=?",
                  (vault, msg_id))


def last_event_ts(vault: str, kinds: tuple) -> float:
    """Timestamp (epoch) do evento mais recente de um dos tipos, ou 0.0."""
    q = f"SELECT MAX(ts) AS t FROM events WHERE vault=? AND kind IN ({','.join('?' * len(kinds))})"
    with _conn() as c:
        row = c.execute(q, (vault, *kinds)).fetchone()
    return float(row["t"] or 0.0)


def kv_get(vault: str, key: str, default=None):
    with _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE vault=? AND key=?", (vault, key)).fetchone()
    return row["value"] if row else default


def kv_set(vault: str, key: str, value) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT INTO kv (vault, key, value) VALUES (?,?,?) "
                  "ON CONFLICT(vault, key) DO UPDATE SET value=excluded.value",
                  (vault, key, str(value)))


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


def forget_vault_index(vault: str) -> None:
    """Esquece o estado de indexação do vault inteiro (troca de modelo de embeddings):
    o próximo reconcile reindexa tudo. O grafo de links é mantido (não depende dos vetores)."""
    with _lock, _conn() as c:
        c.execute("DELETE FROM notes  WHERE vault=?", (vault,))
        c.execute("DELETE FROM chunks WHERE vault=?", (vault,))


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
