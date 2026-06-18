"""
vault_tools.py — Ferramentas de leitura e escrita no vault via CouchDB.
O agente usa estas funções para executar tarefas reais nas notas.
"""

import os
import re
import logging
import requests

log = logging.getLogger(__name__)

COUCHDB_URL  = os.environ.get("COUCHDB_URL", "http://couchdb:5984")
COUCHDB_USER = os.environ.get("COUCHDB_USER", "admin")
COUCHDB_PASS = os.environ.get("COUCHDB_PASSWORD", "")
COUCHDB_DB   = os.environ.get("COUCHDB_DB", "obsidian")
AUTH         = lambda: (COUCHDB_USER, COUCHDB_PASS)


# ── Leitura ───────────────────────────────────────────────────────────────────

def list_notes(folder: str | None = None) -> list[dict]:
    """Lista todas as notas do vault, opcionalmente filtrando por pasta."""
    r = requests.get(
        f"{COUCHDB_URL}/{COUCHDB_DB}/_all_docs",
        params={"include_docs": True},
        auth=AUTH(),
    )
    r.raise_for_status()
    notes = []
    for row in r.json().get("rows", []):
        doc = row.get("doc", {})
        note_id = doc.get("_id", "")
        if note_id.startswith("_") or note_id.startswith("h:"):
            continue
        if folder and not note_id.startswith(folder):
            continue
        notes.append({
            "note_id": note_id,
            "path":    note_id,
            "size":    len(doc.get("content", "") or doc.get("data", "")),
        })
    return notes


def read_note(note_id: str) -> dict:
    """Lê o conteúdo completo de uma nota pelo seu ID/caminho."""
    r = requests.get(f"{COUCHDB_URL}/{COUCHDB_DB}/{note_id}", auth=AUTH())
    if r.status_code == 404:
        return {"error": f"Nota '{note_id}' não encontrada."}
    r.raise_for_status()
    doc = r.json()
    return {
        "note_id": note_id,
        "content": doc.get("content", "") or doc.get("data", ""),
        "rev":     doc.get("_rev"),
    }


# ── Escrita ───────────────────────────────────────────────────────────────────

def write_note(note_id: str, content: str) -> dict:
    """Cria ou sobrescreve uma nota. Se existir, atualiza preservando o _rev."""
    existing = requests.get(f"{COUCHDB_URL}/{COUCHDB_DB}/{note_id}", auth=AUTH())
    payload = {"_id": note_id, "content": content}
    if existing.status_code == 200:
        payload["_rev"] = existing.json().get("_rev")

    r = requests.put(f"{COUCHDB_URL}/{COUCHDB_DB}/{note_id}", json=payload, auth=AUTH())
    r.raise_for_status()
    log.info(f"write_note: {note_id}")
    return {"ok": True, "note_id": note_id}


def move_note(source_id: str, dest_id: str) -> dict:
    """Move uma nota: copia para novo ID e apaga o original."""
    doc = read_note(source_id)
    if "error" in doc:
        return doc

    write_note(dest_id, doc["content"])
    delete_note(source_id)
    log.info(f"move_note: {source_id} → {dest_id}")
    return {"ok": True, "from": source_id, "to": dest_id}


def delete_note(note_id: str) -> dict:
    """Apaga uma nota do CouchDB."""
    r = requests.get(f"{COUCHDB_URL}/{COUCHDB_DB}/{note_id}", auth=AUTH())
    if r.status_code == 404:
        return {"ok": True, "note": note_id}
    r.raise_for_status()
    rev = r.json().get("_rev")
    r2 = requests.delete(
        f"{COUCHDB_URL}/{COUCHDB_DB}/{note_id}",
        params={"rev": rev},
        auth=AUTH(),
    )
    r2.raise_for_status()
    log.info(f"delete_note: {note_id}")
    return {"ok": True, "note_id": note_id}


def update_frontmatter(note_id: str, tags_to_add: list[str]) -> dict:
    """Adiciona tags ao frontmatter YAML da nota. Cria o bloco se não existir."""
    doc = read_note(note_id)
    if "error" in doc:
        return doc

    content = doc["content"]
    tags_str = "\n".join(f"  - {t}" for t in tags_to_add)

    # Já tem frontmatter?
    if content.startswith("---"):
        end = content.find("---", 3)
        if end == -1:
            return {"error": "Frontmatter malformado em " + note_id}
        frontmatter = content[3:end]

        # Já tem campo tags?
        if "tags:" in frontmatter:
            # Adiciona tags novas sem duplicar
            existing_tags = re.findall(r"- (\S+)", frontmatter.split("tags:")[1].split("\n\n")[0])
            new_tags = [t for t in tags_to_add if t not in existing_tags]
            if not new_tags:
                return {"ok": True, "note_id": note_id, "changed": False}
            new_tags_str = "\n".join(f"  - {t}" for t in new_tags)
            # Insere após a última tag existente
            updated_fm = re.sub(
                r"(tags:.*?)(\n\w|\Z)",
                lambda m: m.group(0).replace(m.group(2), f"\n{new_tags_str}{m.group(2)}"),
                frontmatter,
                flags=re.DOTALL,
            )
            new_content = f"---{updated_fm}---{content[end+3:]}"
        else:
            new_content = f"---{frontmatter}tags:\n{tags_str}\n---{content[end+3:]}"
    else:
        new_content = f"---\ntags:\n{tags_str}\n---\n\n{content}"

    write_note(note_id, new_content)
    return {"ok": True, "note_id": note_id, "tags_added": tags_to_add}


def search_notes_by_content(query: str) -> list[dict]:
    """Busca notas cujo conteúdo contém a string (busca simples, case-insensitive)."""
    r = requests.get(
        f"{COUCHDB_URL}/{COUCHDB_DB}/_all_docs",
        params={"include_docs": True},
        auth=AUTH(),
    )
    r.raise_for_status()
    results = []
    q = query.lower()
    for row in r.json().get("rows", []):
        doc = row.get("doc", {})
        note_id = doc.get("_id", "")
        if note_id.startswith("_") or note_id.startswith("h:"):
            continue
        content = doc.get("content", "") or doc.get("data", "")
        if q in content.lower():
            results.append({"note_id": note_id, "snippet": content[:200]})
    return results


# ── Definições das tools para o LLM ──────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "Lista todas as notas do vault. Use para explorar a estrutura antes de agir.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Pasta para filtrar (opcional). Ex: 'personal/analises'",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": "Lê o conteúdo completo de uma nota pelo seu caminho/ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "Caminho completo da nota. Ex: 'personal/analises/titulo.md'"}
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_note",
            "description": "Cria ou sobrescreve o conteúdo de uma nota. Use para criar notas novas ou editar existentes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "Caminho da nota."},
                    "content": {"type": "string", "description": "Conteúdo completo em Markdown."},
                },
                "required": ["note_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_note",
            "description": "Move uma nota de um caminho para outro (renomeia ou reorganiza pasta).",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string", "description": "Caminho atual da nota."},
                    "dest_id":   {"type": "string", "description": "Novo caminho da nota."},
                },
                "required": ["source_id", "dest_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_note",
            "description": "Apaga permanentemente uma nota. Use com cuidado — só para duplicatas confirmadas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "Caminho da nota a apagar."}
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_frontmatter",
            "description": "Adiciona tags ao frontmatter YAML de uma nota. Cria o bloco --- se não existir.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id":     {"type": "string", "description": "Caminho da nota."},
                    "tags_to_add": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de tags a adicionar. Ex: ['politica', 'analise', 'brasil']",
                    },
                },
                "required": ["note_id", "tags_to_add"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_notes_by_content",
            "description": "Busca notas cujo conteúdo contém um texto específico.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Texto a buscar."}
                },
                "required": ["query"],
            },
        },
    },
]

# Mapa nome → função para dispatch
TOOL_FUNCTIONS = {
    "list_notes":              list_notes,
    "read_note":               read_note,
    "write_note":              write_note,
    "move_note":               move_note,
    "delete_note":             delete_note,
    "update_frontmatter":      update_frontmatter,
    "search_notes_by_content": search_notes_by_content,
}
