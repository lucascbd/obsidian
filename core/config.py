"""config.py — configuração central da arquitetura em disco.

Vault em disco (VAULT_ROOT), estado em SQLite (DATA_DIR), ChromaDB e modelo de
embeddings. Substitui a configuração baseada em CouchDB.
"""

import os
import re
from pathlib import Path

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/vaults"))
DATA_DIR   = Path(os.environ.get("DATA_DIR", "/data"))
STATE_DB   = DATA_DIR / "state.db"

CHROMA_HOST = os.environ.get("CHROMADB_HOST", "chromadb")
CHROMA_PORT = int(os.environ.get("CHROMADB_PORT", 8000))

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large")

# Pastas/arquivos que não devem ser indexados nem tratados como notas do usuário.
META_DIR     = "00-meta"
IGNORE_DIRS  = {".obsidian", ".trash", ".git", META_DIR}

_INVALID_COL = re.compile(r"[^a-zA-Z0-9_-]")


def col_name(vault: str) -> str:
    """Uma coleção ChromaDB por vault (nome sanitizado)."""
    col = _INVALID_COL.sub("_", vault)[:63]
    return col or "vault"


def is_meta(note_id: str) -> bool:
    """True se a nota pertence a 00-meta (índices/config do agente)."""
    return META_DIR in Path(note_id).parts
