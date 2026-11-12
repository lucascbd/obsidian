"""watcher.py — observa o vault em disco e mantém o ChromaDB sincronizado.

Substitui o longpoll do CouchDB por watchdog (inotify). Deriva o vault do
primeiro componente do caminho relativo a VAULT_ROOT. Debounce por arquivo
(editores/ignis emitem vários eventos por save). Estado em SQLite.
"""

import os
import sys
import time
import logging
import threading
from pathlib import Path

# core/ fica na raiz do repo: /app no container; $AGENT_HOME quando rodando bare.
sys.path.insert(0, os.environ.get("AGENT_HOME", "/app"))

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from core import state, vault_fs, indexer
from core.config import VAULT_ROOT, IGNORE_DIRS, is_meta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watcher] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DEBOUNCE_SECS = 1.5
VAULT_RESCAN_SECS = 60


def _vault_and_note(path: str):
    """(vault, note_id) de um caminho absoluto, ou (None, None) se fora de escopo."""
    try:
        rel = Path(path).resolve().relative_to(VAULT_ROOT.resolve())
    except Exception:
        return None, None
    parts = rel.parts
    if len(parts) < 2 or not parts[-1].endswith(".md"):
        return None, None
    if set(parts[1:]) & IGNORE_DIRS:
        return None, None
    vault = parts[0]
    note_id = Path(*parts[1:]).as_posix().lower()
    if is_meta(note_id):
        return None, None
    return vault, note_id


class VaultHandler(FileSystemEventHandler):
    """Agenda (re)indexação/remoção com debounce por arquivo."""

    def __init__(self):
        self._pending: dict[str, float] = {}   # path → horário-alvo
        self._deleted: set[str] = set()
        self._lock = threading.Lock()

    def _schedule(self, path: str, deleted: bool = False):
        with self._lock:
            self._pending[path] = time.time() + DEBOUNCE_SECS
            if deleted:
                self._deleted.add(path)
            else:
                self._deleted.discard(path)

    def on_created(self, e):
        if not e.is_directory:
            self._schedule(e.src_path)

    def on_modified(self, e):
        if not e.is_directory:
            self._schedule(e.src_path)

    def on_deleted(self, e):
        if not e.is_directory:
            self._schedule(e.src_path, deleted=True)

    def on_moved(self, e):
        if not e.is_directory:
            self._schedule(e.src_path, deleted=True)
            self._schedule(e.dest_path)

    def process_due(self):
        now = time.time()
        with self._lock:
            due = [p for p, t in self._pending.items() if t <= now]
            for p in due:
                del self._pending[p]
            deleted = {p for p in due if p in self._deleted}
            self._deleted -= deleted

        for path in due:
            vault, note_id = _vault_and_note(path)
            if not vault:
                continue
            try:
                if path in deleted or not Path(path).is_file():
                    indexer.remove_note(vault, note_id)
                else:
                    content = vault_fs.read_path(Path(path))
                    indexer.index_note(vault, note_id, content)
            except Exception as ex:
                log.error(f"[{vault}] erro ao processar {note_id}: {ex}")


def wait_for_chroma():
    import requests
    from core.config import CHROMA_HOST, CHROMA_PORT
    log.info("Aguardando ChromaDB...")
    while True:
        for path in ("/api/v2/heartbeat", "/api/v1/heartbeat"):
            try:
                if requests.get(f"http://{CHROMA_HOST}:{CHROMA_PORT}{path}", timeout=5).status_code == 200:
                    log.info("ChromaDB OK")
                    return
            except Exception:
                pass
        time.sleep(3)


def initial_scan():
    """Reconcilia cada vault no disco com o índice (pula o que não mudou)."""
    for vault in vault_fs.get_vaults():
        try:
            indexer.reconcile(vault)
        except Exception as e:
            log.error(f"[{vault}] erro no scan inicial: {e}")


def main():
    VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    state.init_db()
    wait_for_chroma()

    log.info(f"Scan inicial em {VAULT_ROOT}")
    initial_scan()

    handler = VaultHandler()
    observer = Observer()
    observer.schedule(handler, str(VAULT_ROOT), recursive=True)
    observer.start()
    log.info(f"Observando {VAULT_ROOT} (recursivo)")

    last_rescan = time.time()
    try:
        while True:
            handler.process_due()
            # Rescan periódico: pega vaults novos e qualquer evento perdido.
            if time.time() - last_rescan > VAULT_RESCAN_SECS:
                initial_scan()
                last_rescan = time.time()
            time.sleep(0.5)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
