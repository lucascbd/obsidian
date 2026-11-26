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
from core.config import is_meta
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
    from core import embedder
    from core.config import EMBEDDING_MODEL
    key = hashlib.md5(("|".join(cats) + "#" + EMBEDDING_MODEL).encode()).hexdigest()
    if key in _cat_cache:
        return _cat_cache[key]
    import numpy as np
    texts = [f"categoria: {c.replace('-', ' ')}" for c in cats]
    mat = np.array(embedder.embed(texts), dtype=float)
    mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    _cat_cache[key] = (cats, mat)
    return cats, mat


def _embed(text: str):
    import numpy as np
    from core import embedder
    v = np.array(embedder.embed_one(text[:2000]), dtype=float)
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


def enforce_structure(vault: str, path: str) -> str:
    """Guarda final: garante knowledge/{categoria}/{arquivo}.md (profundidade 2).
    Colapsa qualquer subpasta extra que o LLM tenha inventado. 00-meta fica intocado.
    Se houver taxonomia e um dos segmentos for categoria válida, usa-a como categoria."""
    parts = [p for p in (path or "").strip("/").split("/") if p]
    if not parts:
        return path
    if parts[0] == "00-meta":
        return "/".join(parts)
    if parts[0] != KNOWLEDGE:
        parts = [KNOWLEDGE] + parts
    if len(parts) <= 3:
        # knowledge/categoria/arquivo.md já está ok (ou knowledge/arquivo.md)
        if len(parts) == 3:
            return f"{KNOWLEDGE}/{slugify(parts[1])}/{parts[-1]}"
        return "/".join(parts)
    # profundidade > 2: colapsa. Prefere um segmento que seja categoria válida.
    try:
        instr = load_instructions(vault)
        cats = parse_taxonomy(instr) if instr else []
    except Exception:
        cats = []
    categoria = slugify(parts[1])
    for seg in parts[1:-1]:
        if slugify(seg) in cats:
            categoria = slugify(seg)
            break
    return f"{KNOWLEDGE}/{categoria}/{parts[-1]}"


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


def _current_category(nid: str) -> str:
    parts = nid.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == KNOWLEDGE else ""


def _iter_moves(vault: str, cats: list, instr: str, use_llm: bool, st: dict = None,
                only_ids: set = None):
    """Gera os movimentos UM A UM (quem consome pode aplicar na hora), REUSANDO os
    embeddings já no ChromaDB. Empate (top1 - top2 < MARGIN):
      - se a pasta atual da nota está entre as candidatas → mantém (sem LLM);
      - senão, com use_llm, o LLM desempata; se o limite do Pro estourar, as notas
        empatadas restantes ficam onde estão (o resto segue normalmente).
    `st` (opcional) recebe o progresso e pode pedir cancelamento (st["cancel"])."""
    import numpy as np
    import sdk_llm
    from core.indexer import get_collection
    st = st if st is not None else {}
    col = get_collection(vault)
    names, mat = _vectors(cats)

    # Lê a coleção em páginas e guarda só 1 vetor por nota (evita trazer todos os
    # chunks de uma vez para a memória).
    note_vec, note_meta = {}, {}
    page, offset = 500, 0
    while True:
        data = col.get(include=["embeddings", "metadatas"], limit=page, offset=offset)
        embs = data.get("embeddings")
        metas = data.get("metadatas")
        if embs is None:
            embs = []
        if metas is None:
            metas = []
        if len(metas) == 0:
            break
        for emb, meta in zip(embs, metas):
            nid = (meta or {}).get("note_id", "")
            if not nid or is_meta(nid) or nid in note_vec:
                continue
            if only_ids is not None and nid not in only_ids:
                continue
            note_vec[nid] = np.asarray(emb, dtype=np.float32)
            note_meta[nid] = meta
        offset += len(metas)
        if len(metas) < page:
            break

    # Notas pedidas que o watcher ainda não indexou: ficam para a próxima vez.
    st["pending"] = set(only_ids) - set(note_vec) if only_ids is not None else set()
    st["total"] = len(note_vec)
    st["phase"] = "classificando e movendo"
    rate_limited = False
    for nid, emb in note_vec.items():
        if st.get("cancel"):
            return
        st["processed"] = st.get("processed", 0) + 1
        q = np.asarray(emb, dtype=float)
        if q.shape[0] != mat.shape[1]:
            raise RuntimeError("o índice ainda está com vetores do modelo anterior — "
                               "aguarde o watcher terminar de reindexar e rode de novo")
        q = q / (np.linalg.norm(q) + 1e-9)
        sims = mat @ q
        order = list(np.argsort(-sims)[:TOP_K])
        sl = [(names[i], float(sims[i])) for i in order]
        meta = note_meta[nid]
        # Sem title/date no frontmatter, usa o nome do arquivo — tirando o prefixo de
        # data, senão cada execução renomearia de novo (2026-11-26-2026-11-26-...).
        stem = nid.split("/")[-1][:-3] if nid.endswith(".md") else nid.split("/")[-1]
        m_date = re.match(r"(\d{4}-\d{2}-\d{2})-?(.*)$", stem)
        title = meta.get("title") or (m_date.group(2) if m_date and m_date.group(2) else stem)
        date = str(meta.get("date") or (m_date.group(1) if m_date else ""))
        cands = [c for c, _ in sl]
        cur = _current_category(nid)

        if len(sl) > 1 and (sl[0][1] - sl[1][1]) < MARGIN:
            if cur in cands:
                cat = cur
                st["kept_tie"] = st.get("kept_tie", 0) + 1
            elif use_llm and not rate_limited:
                try:
                    content = vault_fs.read_note(vault, nid)
                except Exception:
                    content = ""
                try:
                    cat = _llm_tiebreak(title, meta.get("source", ""), content, cands, instr) or sl[0][0]
                    st["llm"] = st.get("llm", 0) + 1
                except sdk_llm.LLMRateLimited:
                    rate_limited = True
                    st["skipped_rate"] = st.get("skipped_rate", 0) + 1
                    st.setdefault("skipped_ids", set()).add(nid)
                    log.warning("[reorg] limite do LLM — empates restantes ficam onde estão")
                    continue
            elif use_llm:
                st["skipped_rate"] = st.get("skipped_rate", 0) + 1
                st.setdefault("skipped_ids", set()).add(nid)
                continue
            else:
                cat = sl[0][0]
        else:
            cat = sl[0][0]

        target = build_path(cat, title, date)
        if target.lower() != nid.lower():
            yield {"from": nid, "to": target, "category": cat}


def _compute_moves(vault: str, cats: list, instr: str, use_llm: bool, limit: int) -> list:
    moves = []
    for m in _iter_moves(vault, cats, instr, use_llm):
        moves.append(m)
        if len(moves) >= limit:
            break
    return moves


def keep_dirs(cats: list) -> set:
    """Pastas que nunca são apagadas por estarem vazias: knowledge/ e as categorias fixas."""
    return {KNOWLEDGE} | {f"{KNOWLEDGE}/{c}" for c in cats}


def finalize(vault: str) -> dict:
    """Depois de mover notas: refaz os índices (00-meta/indices) e apaga pastas vazias."""
    import agent
    instr = load_instructions(vault)
    cats = parse_taxonomy(instr) if instr else []
    n_notes, n_subs = agent._build_vault_indices(vault)
    removed = vault_fs.remove_empty_dirs(vault, keep=keep_dirs(cats))
    return {"notes": n_notes, "subs": n_subs, "removed_dirs": removed}


def organize_notes(vault: str, ids: set, st: dict = None) -> dict:
    """Organiza (classifica + move) SÓ as notas em `ids`. Usado pela manutenção
    incremental. Retorna contadores e as notas ainda não indexadas (pending)."""
    st = st if st is not None else {}
    st.update(processed=0, moved=0, llm=0, kept_tie=0, skipped_rate=0, errors=0, last="",
              skipped_ids=set())
    instr = load_instructions(vault)
    cats = parse_taxonomy(instr) if instr else []
    if not cats or not ids:
        return {"moved": 0, "analyzed": 0, "llm": 0, "skipped_rate": 0, "pending": set(ids or ())}
    for m in _iter_moves(vault, cats, instr, use_llm=True, st=st, only_ids=set(ids)):
        if st.get("cancel"):
            break
        try:
            _apply_move(vault, m["from"], m["to"])
            st["moved"] += 1
            st["last"] = m["to"]
        except Exception as e:
            st["errors"] += 1
            log.error(f"[reorg] erro em {m['from']}: {e}")
    return {"moved": st["moved"], "analyzed": st.get("processed", 0), "llm": st.get("llm", 0),
            "skipped_rate": st.get("skipped_rate", 0),
            "pending": set(st.get("pending", set())) | set(st.get("skipped_ids", set()))}


_applying = {"running": False, "cancel": False, "thread": None}


def cancel() -> bool:
    """Pede para a organização em andamento parar (após a nota atual)."""
    if _applying.get("running"):
        _applying["cancel"] = True
        return True
    return False


def _fmt_status(st: dict) -> str:
    import time
    mins = int((time.time() - st.get("started", time.time())) // 60)
    lines = [
        f"Notas analisadas: **{st.get('processed', 0)}/{st.get('total', 0) or '?'}**",
        f"Movidas: **{st.get('moved', 0)}**" + (f" · erros: {st['errors']}" if st.get("errors") else ""),
        f"Empates mantidos na pasta atual: {st.get('kept_tie', 0)} · desempates pelo LLM: {st.get('llm', 0)}",
    ]
    if st.get("removed_dirs"):
        lines.append(f"Pastas vazias removidas: {st['removed_dirs']} · índices refeitos")
    if st.get("skipped_rate"):
        lines.append(f"⚠️ Limite do Claude atingido: {st['skipped_rate']} empate(s) ficaram onde estavam")
    if st.get("last"):
        lines.append(f"Última: `{st['last']}`")
    lines.append(f"_{st.get('phase', '')} — {mins} min_")
    return "\n".join(lines)


def status() -> dict:
    """Progresso da organização (para a UI acompanhar)."""
    st = _applying
    t = st.get("thread")
    if st.get("running") and t is not None and not t.is_alive():
        st["running"] = False  # thread morreu sem limpar o estado
    return {"running": bool(st.get("running")), "result": st.get("result", ""),
            "text": _fmt_status(st) if st.get("started") else "",
            "processed": st.get("processed", 0), "total": st.get("total", 0),
            "moved": st.get("moved", 0)}


def _apply_worker(vault: str):
    import time
    st = _applying
    st.update(running=True, cancel=False, vault=vault, phase="lendo o índice", total=0,
              processed=0, moved=0, llm=0, kept_tie=0, skipped_rate=0, errors=0,
              removed_dirs=0, started=time.time(), last="", result="")
    try:
        instr = load_instructions(vault)
        cats = parse_taxonomy(instr) if instr else []
        for m in _iter_moves(vault, cats, instr, use_llm=True, st=st):
            if st.get("cancel"):
                break
            try:
                _apply_move(vault, m["from"], m["to"])
                st["moved"] += 1
                st["last"] = m["to"]
            except Exception as e:
                st["errors"] += 1
                log.error(f"[reorg] erro em {m['from']}: {e}")
            if st["processed"] % 25 == 0:
                log.info(f"[reorg] {st['processed']}/{st['total']} analisadas, {st['moved']} movidas")
        st["phase"] = "refazendo índices e limpando pastas vazias"
        try:
            fin = finalize(vault)
            st["removed_dirs"] = len(fin["removed_dirs"])
        except Exception as e:
            log.error(f"[reorg] erro ao refazer índices/pastas: {e}")
        st["result"] = "cancelado" if st.get("cancel") else "concluído"
        st["phase"] = st["result"]
        log.info(f"[reorg] {st['result']}: {st['moved']} movida(s) de {st['processed']}/{st['total']} "
                 f"analisada(s); {st['llm']} desempate(s) LLM, {st['kept_tie']} mantida(s)")
    except Exception as e:
        st["result"] = f"erro: {e}"
        st["phase"] = st["result"]
        log.error(f"[reorg] worker erro: {e}")
    finally:
        st["running"] = False


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

    if status()["running"]:
        return {"answer": "⏳ A organização já está rodando:\n\n" + _fmt_status(_applying),
                "moves": [], "sources": [], "running": True}
    import threading
    t = threading.Thread(target=_apply_worker, args=(vault,), daemon=True, name="reorg-apply")
    _applying["thread"] = t
    t.start()
    return {"answer": "🗂 Organização iniciada. Cada nota é movida assim que é classificada "
                      "(os wiki links são reescritos junto). O ⛔ interrompe.",
            "moves": [], "sources": [], "running": True}
