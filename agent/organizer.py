"""organizer.py — classificação e organização de notas conforme instructions.md.

ChromaDB faz o atalho: embeda as categorias (taxonomia fixa) uma vez e roteia a
nota pela categoria mais próxima — sem reler o vault. O LLM só DESEMPATA quando
os candidatos estão perto demais. A estrutura (profundidade, nome de arquivo) é
validada em código, não "pedida" ao LLM.

Fonte da política: 00-meta/config/instructions.md
"""

import re
import logging
import hashlib
import unicodedata
import datetime

from core import vault_fs
from core.text import parse_frontmatter

log = logging.getLogger(__name__)

INSTRUCTIONS_PATHS = ["00-meta/config/instructions.md", "00-meta/config/instructions/index.md"]
KNOWLEDGE = "knowledge"
TOP_K  = 3
MARGIN = 0.05   # se top1 - top2 >= margin, decide sem LLM

_cat_cache: dict = {}   # hash(taxonomia) -> (names, matrix)


# ── Instruções / taxonomia ──────────────────────────────────────────────────────

def load_instructions(vault: str) -> str:
    for p in INSTRUCTIONS_PATHS:
        try:
            if vault_fs.exists(vault, p):
                return vault_fs.read_note(vault, p)
        except Exception:
            pass
    return ""


def parse_taxonomy(text: str) -> list:
    """Extrai a lista fixa de categorias sob `knowledge/` da árvore em instructions.md."""
    blocks = re.findall(r"```[a-zA-Z]*\n(.*?)```", text, re.DOTALL)
    tree = next((b for b in blocks if "knowledge/" in b), "")
    if not tree:
        idx = text.find("knowledge/")
        tree = text[idx:] if idx >= 0 else ""

    cats, started, seen = [], False, set()
    for line in tree.splitlines():
        if "knowledge/" in line:
            started = True
            continue
        if not started:
            continue
        m = re.search(r"([a-z0-9][a-z0-9\-]+)/\s*$", line.strip())
        if m:
            name = m.group(1)
            if name not in ("config", "agent-mem", "instructions", "00-meta", "knowledge") and name not in seen:
                seen.add(name)
                cats.append(name)
    return cats


def _priority_rules(text: str) -> str:
    """Seção de critérios de classificação (para o desempate do LLM)."""
    m = re.search(r"(?is)#+\s*Crit[eé]rios de Classifica[cç][aã]o(.+?)(?:\n#\s|\Z)", text)
    return (m.group(1).strip() if m else text[:1500])[:1500]


# ── Embeddings / roteamento (ChromaDB) ──────────────────────────────────────────

def _vectors(cats: list):
    key = hashlib.md5("|".join(cats).encode()).hexdigest()
    if key in _cat_cache:
        return _cat_cache[key]
    import numpy as np
    from core.indexer import get_model
    texts = [f"categoria: {c.replace('-', ' ')}" for c in cats]
    mat = np.array([list(v) for v in get_model().embed(texts)], dtype=float)
    mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    _cat_cache[key] = (cats, mat)
    return cats, mat


def _embed(text: str):
    import numpy as np
    from core.indexer import get_model
    v = list(get_model().embed([text[:2000]]))[0]
    v = np.array(list(v), dtype=float)
    return v / (np.linalg.norm(v) + 1e-9)


def shortlist(text: str, cats: list, k: int = TOP_K) -> list:
    """Top-k categorias mais próximas (nome, similaridade)."""
    names, mat = _vectors(cats)
    q = _embed(text)
    sims = mat @ q
    import numpy as np
    order = list(np.argsort(-sims)[:k])
    return [(names[i], float(sims[i])) for i in order]


def _llm_tiebreak(title, summary, note_text, candidates, instructions) -> str:
    """Desempate: o LLM escolhe UMA categoria entre os candidatos, pelas regras."""
    import sdk_llm
    system = ("Você classifica uma nota em UMA categoria. Responda APENAS com o nome "
              "exato de uma das categorias candidatas — uma palavra, nada mais.")
    user = (f"REGRAS DE PRIORIDADE:\n{_priority_rules(instructions)}\n\n"
            f"NOTA:\nTítulo: {title}\nResumo: {summary}\nTrecho: {note_text[:800]}\n\n"
            f"CANDIDATAS: {', '.join(candidates)}\n\n"
            f"Responda só o nome de UMA categoria candidata.")
    out = (sdk_llm.complete(system, user) or "").strip().lower()
    for c in candidates:
        if c in out:
            return c
    return ""


def classify(note_text: str, title: str, summary: str, instructions: str, cats: list,
             use_llm: bool = True) -> str:
    """Categoria da nota: Chroma faz o atalho; LLM só desempata (se use_llm)."""
    sl = shortlist(f"{title} {summary} {note_text}", cats)
    if not sl:
        return "others" if "others" in cats else (cats[0] if cats else "others")
    if not use_llm or len(sl) == 1 or (sl[0][1] - sl[1][1]) >= MARGIN:
        return sl[0][0]
    cand = [c for c, _ in sl]
    return _llm_tiebreak(title, summary, note_text, cand, instructions) or sl[0][0]


# ── Estrutura (determinística) ──────────────────────────────────────────────────

def slugify(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower().strip())
    return re.sub(r"-+", "-", s).strip("-")[:80] or "nota"


def build_path(category: str, title: str, date: str) -> str:
    """knowledge/{categoria}/{aaaa-mm-dd}-{titulo}.md — profundidade e nome forçados."""
    d = (date or "")[:10]
    if not re.match(r"\d{4}-\d{2}-\d{2}$", d):
        d = datetime.date.today().isoformat()
    return f"{KNOWLEDGE}/{slugify(category)}/{d}-{slugify(title)}.md"


# ── API de alto nível ────────────────────────────────────────────────────────────

def classify_note(vault: str, note_text: str, title: str = "", summary: str = "", date: str = "") -> dict:
    """Retorna {'category','path'} ou None se não houver taxonomia."""
    instr = load_instructions(vault)
    cats = parse_taxonomy(instr) if instr else []
    if not cats:
        return None
    cat = classify(note_text, title or summary or "nota", summary, instr, cats)
    return {"category": cat, "path": build_path(cat, title or summary or "nota", date)}


def _rewrite_inbound_links(vault: str, old_id: str, new_id: str) -> int:
    """Atualiza os wiki links que apontavam para a nota movida (usa o grafo do SQLite)."""
    from core import state
    old = old_id if old_id.endswith(".md") else old_id + ".md"
    old_noext = old[:-3]
    new_noext = (new_id[:-3] if new_id.endswith(".md") else new_id)
    changed = 0
    for inc in state.incoming(vault, old):
        try:
            c = vault_fs.read_note(vault, inc)
        except Exception:
            continue
        c2 = re.sub(r"(\[\[)" + re.escape(old_noext) + r"(\.md)?",
                    r"\1" + new_noext, c, flags=re.IGNORECASE)
        if c2 != c:
            vault_fs.write_note(vault, inc, c2)
            changed += 1
    return changed


def _apply_move(vault: str, src: str, dst: str) -> dict:
    from core import state, indexer
    new_id = vault_fs.move_note(vault, src, dst)
    links = _rewrite_inbound_links(vault, src, new_id)
    try:
        indexer.remove_note(vault, src if src.endswith(".md") else src + ".md")
        indexer.index_note(vault, new_id, vault_fs.read_note(vault, new_id), force=True)
    except Exception:
        pass
    state.record_event(vault, actor="agent", kind="reorg",
                       summary=f"Reorganizou {src} → {new_id}" + (f" ({links} link(s) atualizados)" if links else ""),
                       source="vault", ref=new_id)
    return {"from": src, "to": new_id, "links_updated": links}


def _compute_moves(vault: str, cats: list, instr: str, use_llm: bool, limit: int) -> list:
    """Calcula os movimentos REUSANDO os embeddings já no ChromaDB (sem reler/re-embed).
    O LLM só é chamado (se use_llm) nos empates."""
    import numpy as np
    from core.indexer import get_collection
    col = get_collection(vault)
    data = col.get(include=["embeddings", "metadatas"])
    names, mat = _vectors(cats)

    embs = data.get("embeddings")
    metas = data.get("metadatas")
    if embs is None:
        embs = []
    if metas is None:
        metas = []

    note_vec, note_meta = {}, {}
    for emb, meta in zip(embs, metas):
        nid = meta.get("note_id", "")
        if not nid or is_meta(nid) or nid in note_vec:
            continue
        note_vec[nid] = emb
        note_meta[nid] = meta

    moves = []
    for nid, emb in note_vec.items():
        q = np.array(list(emb), dtype=float)
        q = q / (np.linalg.norm(q) + 1e-9)
        sims = mat @ q
        order = list(np.argsort(-sims)[:TOP_K])
        sl = [(names[i], float(sims[i])) for i in order]
        meta = note_meta[nid]
        title = meta.get("title") or nid.split("/")[-1].replace(".md", "")
        date = meta.get("date") or ""

        if use_llm and len(sl) > 1 and (sl[0][1] - sl[1][1]) < MARGIN:
            try:
                content = vault_fs.read_note(vault, nid)
            except Exception:
                content = ""
            cat = _llm_tiebreak(title, meta.get("source", ""), content,
                                [c for c, _ in sl], instr) or sl[0][0]
        else:
            cat = sl[0][0]

        target = build_path(cat, title, date)
        if target.lower() != nid.lower():
            moves.append({"from": nid, "to": target, "category": cat})
            if len(moves) >= limit:
                break
    return moves


_applying = {"running": False}


def _apply_worker(vault: str):
    _applying["running"] = True
    try:
        instr = load_instructions(vault)
        cats = parse_taxonomy(instr) if instr else []
        moves = _compute_moves(vault, cats, instr, use_llm=True, limit=100000)
        log.info(f"[reorg] aplicando {len(moves)} move(s)…")
        done = 0
        for m in moves:
            try:
                _apply_move(vault, m["from"], m["to"])
                done += 1
            except Exception as e:
                log.error(f"[reorg] erro em {m['from']}: {e}")
        log.info(f"[reorg] concluído: {done}/{len(moves)} nota(s) reorganizada(s)")
    except Exception as e:
        log.error(f"[reorg] worker erro: {e}")
    finally:
        _applying["running"] = False


def reorg(vault: str, apply: bool = False, limit: int = 300) -> dict:
    """Reorganiza as notas conforme instructions.md, reusando os embeddings do Chroma.
    apply=False → simula rápido (só Chroma). apply=True → roda em background (desempate
    com LLM, move e reescreve os wiki links)."""
    instr = load_instructions(vault)
    cats = parse_taxonomy(instr) if instr else []
    if not cats:
        return {"answer": "Sem taxonomia em `00-meta/config/instructions.md` — nada a organizar.\n\n"
                          "Crie esse arquivo com a árvore de categorias sob `knowledge/`.",
                "moves": [], "sources": []}

    if not apply:
        moves = _compute_moves(vault, cats, instr, use_llm=False, limit=limit)
        lines = [f"**{len(moves)} nota(s) a reorganizar** (simulação — nada foi movido):", ""]
        lines += [f"- `{m['from']}` → `{m['to']}`" for m in moves[:60]]
        if len(moves) > 60:
            lines.append(f"- _(+{len(moves) - 60} … rode o aplicar p/ o restante)_")
        return {"answer": "\n".join(lines), "moves": moves, "sources": []}

    if _applying["running"]:
        return {"answer": "⏳ Organização já está rodando em background.", "moves": [], "sources": []}
    import threading
    threading.Thread(target=_apply_worker, args=(vault,), daemon=True, name="reorg-apply").start()
    return {"answer": "🗂 Organização iniciada em background. As notas vão sendo movidas e os "
                      "wiki links reescritos — acompanhe no vault (e nos eventos `reorg`). "
                      "O log do agente mostra `[reorg] concluído: N/M`.",
            "moves": [], "sources": []}
