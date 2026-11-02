"""settings.py — configurações do app persistidas em disco (./data/settings.json).

Guarda o modelo LLM escolhido e os horários de reindexação do ChromaDB.
Simples e sem dependências: leitura/escrita de um JSON.
"""

import os
import json
import threading

from .config import DATA_DIR

_SETTINGS_FILE = DATA_DIR / "settings.json"
_lock = threading.Lock()

_DEFAULTS = {
    "model": os.environ.get("ANTHROPIC_MODEL", "sonnet"),
    "reindex_times": [],   # ["03:00", "15:00"] — horários HH:MM (hora local do container)
}

_VALID_MODELS = {"sonnet", "opus", "haiku"}


def get_settings() -> dict:
    data = dict(_DEFAULTS)
    try:
        if _SETTINGS_FILE.exists():
            data.update(json.loads(_SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    # saneamento
    if data.get("model") not in _VALID_MODELS:
        data["model"] = _DEFAULTS["model"]
    times = data.get("reindex_times") or []
    data["reindex_times"] = [t for t in times if _valid_time(t)]
    return data


def save_settings(patch: dict) -> dict:
    with _lock:
        data = get_settings()
        if "model" in patch and patch["model"] in _VALID_MODELS:
            data["model"] = patch["model"]
        if "reindex_times" in patch and isinstance(patch["reindex_times"], list):
            data["reindex_times"] = sorted({t for t in patch["reindex_times"] if _valid_time(t)})
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _valid_time(t) -> bool:
    if not isinstance(t, str) or len(t) != 5 or t[2] != ":":
        return False
    try:
        h, m = int(t[:2]), int(t[3:])
        return 0 <= h <= 23 and 0 <= m <= 59
    except Exception:
        return False
