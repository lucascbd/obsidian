"""
agent.py — Agente com tool calling: lê e escreve no vault em disco + ChromaDB.
"""

import os
import re
import io
import json
import logging
import unicodedata
import requests
from pathlib import Path
from typing import Optional

from core import vault_fs, state, indexer, embedder
from core.config import VAULT_ROOT, DATA_DIR, EMBEDDING_MODEL, col_name as _col_name, is_meta
from core.text import (
    parse_frontmatter as _parse_frontmatter_core,
    chunk_by_sections,
    normalize_tag as _normalize_tag,
    fix_codeblock_frontmatter as _fix_codeblock_frontmatter,
    fix_tags_in_frontmatter as _fix_tags_in_frontmatter,
)

log = logging.getLogger(__name__)

# LLM via Claude Agent SDK (Fase 2). Ver agent/sdk_llm.py.
import sdk_llm

# Vault padrão: primeiro subdiretório de VAULT_ROOT, ou o configurado.
DEFAULT_VAULT = os.environ.get("DEFAULT_VAULT", "")


def _default_vault() -> str:
    if DEFAULT_VAULT:
        return DEFAULT_VAULT
    vaults = vault_fs.get_vaults()
    return vaults[0] if vaults else "personal"

MAX_ROUNDS = 500

_stop_event   = __import__("threading").Event()


# Embeddings: core.embedder (o agent é o único processo que carrega o modelo).
def get_chroma():
    return indexer.get_chroma()


def stop_agent():
    """Sinaliza o loop de tool calling para parar."""
    _stop_event.set()


def reset_stop():
    """Limpa o sinal de stop antes de iniciar nova operação."""
    _stop_event.clear()

# Palavras-chave que indicam trabalho pendente na resposta de "done"
_PENDING_PATTERNS = [
    r"preciso continuar",
    r"ainda restam",
    r"pr[oó]ximas? notas?",
    r"continuarei",
    r"continuando",
    r"processar(ei)? as? (pr[oó]ximas?|demais|restantes)",
    r"nas pr[oó]ximas? itera[cç][oõ]es?",
    r"faltam \d+",
    r"pendentes?",
]
_PENDING_RE = re.compile("|".join(_PENDING_PATTERNS), re.IGNORECASE)


# ── Vault em disco ──────────────────────────────────────────────────────────────
def _vault(db: str = None) -> str:
    """Resolve o nome do vault a usar (subdiretório de VAULT_ROOT)."""
    return db or _default_vault()


def get_vaults() -> list:
    """Cada subdiretório de VAULT_ROOT é um vault."""
    vaults = vault_fs.get_vaults()
    log.info(f"get_vaults: {vaults}")
    return vaults


def get_root_folders() -> list:
    """Alias de get_vaults() para compatibilidade."""
    return get_vaults()


# ── Sistema 00-meta/config/ ───────────────────────────────────────────────────
DEFAULT_PROFILE_MD = """# Perfil do Agente

Você é o Mordomo do Conhecimento — um agente autônomo que organiza, conecta e enriquece o vault Obsidian do usuário.

## Persona
- Organizado, proativo e direto
- Executa tarefas até o fim sem pedir confirmação desnecessária
- Usa linguagem clara e objetiva em português

## Comportamento
- Antecipa necessidades do usuário
- Mantém consistência estrutural no vault
- Prefere ação a análise excessiva
"""

DEFAULT_LOAD_MD = """# Regras de Ingestão

## Estrutura de Pastas
- Artigos e referências → `referencias/tema/nome.md`
- Reuniões e atas → `reunioes/YYYY/nome-reuniao.md`
- Projetos → `projetos/nome-projeto/`
- Análises e estudos → `analises/tema/nome.md`
- Perfis de empresas → `empresas/nome-empresa/perfil.md`
- Relatórios de mercado → `mercado/regiao/nome-relatorio.md`

## Frontmatter Obrigatório
```yaml
---
title: Título da Nota
date: YYYY-MM-DD
source: URL ou nome do arquivo
tags:
  - tag1
  - tag2
---
```

## Qualidade
- Slug em lowercase com hifens, sem acentos
- Seções organizadas com ## e ###
- Wiki links com alias: [[caminho/nota|Nome Visível]]
- Conteúdo em português, conciso e navegável
"""


def load_settings(vault: str, db: str = None) -> dict:
    """Lê as notas de config do vault em 00-meta/config/. Retorna dict com profile, load e mem."""
    settings = {"profile": "", "load": "", "mem": ""}
    mapping = {
        "profile": "00-meta/config/profile.md",
        "load":    "00-meta/config/load.md",
        "mem":     "00-meta/config/agent-mem.md",
    }
    v = _vault(db)
    for key, note_id in mapping.items():
        try:
            if vault_fs.exists(v, note_id):
                settings[key] = vault_fs.read_note(v, note_id)
        except Exception:
            pass
    return settings


def save_session_memory(vault: str, question: str, answer: str, sources: list,
                        actions_summary: str, db: str = None) -> None:
    """Faz append em 00-meta/config/agent-mem.md com o resumo da sessão."""
    import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M") + " UTC"
    note_id = "00-meta/config/agent-mem.md"

    notes_bullets = "\n".join(f"- {s}" for s in sources) if sources else "- (nenhuma nota afetada)"
    new_entry = (
        f"\n## Sessão {now}\n"
        f"**Vault:** {vault}\n"
        f"**Tarefa:** {question}\n"
        f"**Ações:** {actions_summary}\n"
        f"**Notas afetadas:**\n{notes_bullets}\n"
        f"---\n"
    )

    v = _vault(db)
    try:
        existing = vault_fs.read_note(v, note_id) if vault_fs.exists(v, note_id) else "# Memória de Sessões\n"
        vault_fs.write_note(v, note_id, existing + new_entry)
    except Exception:
        tool_create_note(note_id, f"# Memória de Sessões\n{new_entry}", db=db)


def tool_ensure_settings(vault: str, db: str = None) -> str:
    """Cria 00-meta/config/profile.md e load.md com conteúdo padrão se não existirem."""
    v = _vault(db)
    created = []
    for note_id, default_content in [
        ("00-meta/config/profile.md", DEFAULT_PROFILE_MD),
        ("00-meta/config/load.md",    DEFAULT_LOAD_MD),
    ]:
        if vault_fs.exists(v, note_id):
            continue
        result = tool_create_note(note_id, default_content, db=db)
        created.append(f"{note_id}: {result}")

    if not created:
        return "Arquivos de settings já existem em '00-meta/config/'."
    return "Settings criados:\n" + "\n".join(created)


# ── Tools disponíveis para o agente ──────────────────────────────────────────
def purge_vault(keep_prefix: Optional[str] = None, db: str = None) -> dict:
    """Apaga do disco todas as notas do vault (exceto 00-meta e, se dado, keep_prefix)."""
    v = _vault(db)
    purged, skipped = [], []
    for note_id in vault_fs.list_notes(v):
        if keep_prefix and note_id.startswith(keep_prefix):
            skipped.append(note_id)
            continue
        try:
            if vault_fs.delete_note(v, note_id):
                indexer.remove_note(v, note_id)
                purged.append(note_id)
        except Exception as e:
            log.error(f"Erro ao purgar {note_id}: {e}")
    return {
        "answer": f"{len(purged)} nota(s) removida(s) do disco.\n" + "\n".join(f"- {n}" for n in purged),
        "sources": purged,
        "skipped": skipped,
    }


def _parse_frontmatter(content: str) -> dict:
    return _parse_frontmatter_core(content)


def reindex_vault(vault: Optional[str] = None) -> dict:
    """Reconstrói do zero a coleção ChromaDB a partir do disco e regenera os índices Obsidian."""
    vaults_to_run = [vault] if vault else get_vaults()
    results = []

    for v in vaults_to_run:
        col_nm = _col_name(v)
        chroma = get_chroma()
        # Recria a coleção e limpa o estado SQLite (reindexação forçada total).
        try:
            chroma.delete_collection(col_nm)
        except Exception:
            pass
        for note_id in list(state.known_paths(v)):
            state.forget_note(v, note_id)

        col = indexer.get_collection(v)   # cria gravando o modelo atual nos metadados
        stats = indexer.reconcile(v, col=col, force=True)
        results.append(f"[{v}] {stats['indexed']} nota(s) reindexadas")
        state.record_event(v, actor="system", kind="reindex",
                           summary=f"Reindexou o ChromaDB ({stats['indexed']} notas)", source="chromadb")

        try:
            _build_vault_indices(v, col)
            results.append(f"[{v}] índices Obsidian gerados em 00-meta/indices/")
        except Exception as e:
            log.warning(f"[reindex] Erro ao gerar índices: {e}")

    return {
        "answer": "✅ Reindexação concluída:\n" + "\n".join(f"- {r}" for r in results),
        "sources": [],
    }


MAINT_KEY = "last_maintenance_ts"
_maint_lock = __import__("threading").Lock()
_maint_job = {"running": False, "thread": None, "phase": "", "answer": "", "result": "", "started": 0.0}


def _last_maintenance(vault: str) -> float:
    """Momento da última manutenção. Na 1ª vez, usa o histórico (último evento de
    índices/correlação) para não reprocessar o vault inteiro; sem histórico → 0 (tudo)."""
    ts = state.kv_get(vault, MAINT_KEY)
    if ts:
        try:
            return float(ts)
        except ValueError:
            pass
    return state.last_event_ts(vault, ("maintenance", "indices", "correlate"))


def _maintain_vault(v: str, full: bool = False, st: dict = None) -> str:
    """Manutenção INCREMENTAL de um vault:
    1. organiza (classifica + move) só as notas novas/alteradas desde a última vez;
    2. 'Notas Relacionadas' só dessas notas (+ notas com link quebrado);
    3. refaz os índices 00-meta/indices (do disco, barato) e apaga pastas vazias.
    O LLM só é usado no passo 1, e só em empates de categoria."""
    import time
    import organizer
    st = st if st is not None else {}
    t0 = time.time()
    vault_fs.start_write_log()
    try:
        return _maintain_vault_inner(v, full, st, t0)
    finally:
        vault_fs.stop_write_log()


def _maintain_vault_inner(v: str, full: bool, st: dict, t0: float) -> str:
    import time
    import organizer
    since = 0.0 if full else _last_maintenance(v)
    when = time.strftime("%d/%m %H:%M", time.localtime(since)) if since else ""

    # 0. Garante o índice: indexa aqui o que o watcher ainda não indexou (só o que
    #    mudou — notas iguais são puladas pelo hash). Sem isso, notas recém-chegadas
    #    não podem ser organizadas nem relacionadas.
    st["phase"] = "conferindo o índice das notas"
    try:
        indexer.reconcile(v)
    except Exception as e:
        log.warning(f"[maintenance] reconcile em {v}: {e}")

    notes = _disk_notes(v)
    new_ids = {n["id"] for n in notes if n["mtime"] > since}
    if full:
        head = f"Vault inteiro: **{len(notes)}** nota(s)"
    elif since:
        head = f"Notas novas desde {when}: **{len(new_ids)}**"
    else:
        head = f"Primeira manutenção: **{len(new_ids)}** nota(s)"
    lines = [f"✅ **Manutenção concluída** — vault `{v}`", head]

    pending, moved = set(), 0
    if new_ids:
        st["phase"] = f"adequando {len(new_ids)} nota(s) ao instructions.md"
        org = organizer.organize_notes(v, new_ids, st)
        pending, moved = org["pending"], org["moved"]
        in_place = max(org["analyzed"] - moved, 0)
        lines.append(f"- 🗂 {moved} movida(s) para a pasta certa · {in_place} já estavam no lugar")
        # Mover preserva a data de modificação: recalcula os ids novos após os movimentos.
        notes = _disk_notes(v)
        new_ids = {n["id"] for n in notes if n["mtime"] > since}

    st["phase"] = "atualizando Notas Relacionadas"
    rv = vault_review(v, only_ids=None if full else new_ids)
    if rv.get("edited"):
        lines.append(f"- 🔗 Notas Relacionadas atualizadas em {len(rv['edited'])} nota(s)")

    st["phase"] = "refazendo índices e apagando pastas vazias"
    fin = organizer.finalize(v)
    lines.append(f"- 🗺 Índices refeitos: {fin['notes']} nota(s) em {fin['subs']} sub-tema(s)")
    if fin["removed_dirs"]:
        shown = ", ".join(f"`{d}`" for d in fin["removed_dirs"][:8])
        more = f" (+{len(fin['removed_dirs']) - 8})" if len(fin["removed_dirs"]) > 8 else ""
        lines.append(f"- 🧹 Pastas vazias apagadas: {shown}{more}")

    # Próximo ponto de partida = FIM desta manutenção (o que ela mesma escreveu não
    # conta como novo). Recuam o ponto, para entrarem na próxima: notas alteradas por
    # outros durante a execução (ex.: email que chegou), notas ainda sem índice e
    # notas puladas pelo limite do Claude.
    written = getattr(vault_fs._tl, "log", None) or set()
    final = _disk_notes(v)
    mt = {n["id"]: n["mtime"] for n in final}
    late = [n["mtime"] for n in final
            if n["mtime"] > t0 and n["id"] not in written and n["id"] not in new_ids]
    held = [mt[i] for i in pending if i in mt]
    mark = time.time()
    if late or held:
        mark = min(late + held) - 1
    if pending:
        lines.append(f"- ⏳ {len(pending)} nota(s) ficam para a próxima "
                     f"(ainda sem índice ou limite do Claude atingido)")
    state.kv_set(v, MAINT_KEY, mark)
    state.record_event(v, actor="system", kind="maintenance",
                       summary=f"Manutenção do vault ({'inteiro' if full else f'{len(new_ids)} nota(s) nova(s)'}, "
                               f"{moved} movida(s))", source="obsidian")
    return "\n".join(lines)


def obsidian_maintenance(vault: Optional[str] = None, full: bool = False, st: dict = None) -> dict:
    """Manutenção do Obsidian (incremental por padrão; full=True processa todas as notas).
    Uma por vez: se já houver uma rodando, não inicia outra."""
    if not _maint_lock.acquire(blocking=False):
        return {"answer": "⏳ Já existe uma manutenção em andamento.", "sources": []}
    try:
        parts = []
        for v in ([vault] if vault else get_vaults()):
            try:
                parts.append(_maintain_vault(v, full=full, st=st))
            except Exception as e:
                log.error(f"[maintenance] erro em {v}: {e}")
                parts.append(f"❌ **Manutenção com erro** — vault `{v}`: {e}")
        return {"answer": "\n\n".join(parts), "sources": []}
    finally:
        _maint_lock.release()


def start_maintenance(vault: Optional[str] = None, full: bool = False) -> dict:
    """Roda a manutenção em background (a UI acompanha por maintenance_status)."""
    import time
    import threading
    job = _maint_job
    t = job.get("thread")
    if job.get("running") and t is not None and t.is_alive():
        return {"running": True, "answer": "⏳ A manutenção já está rodando."}

    def _run():
        try:
            job["answer"] = obsidian_maintenance(vault, full=full, st=job)["answer"]
            job["result"] = "concluída"
        except Exception as e:
            job["answer"], job["result"] = f"❌ Erro na manutenção: {e}", "com erro"
        finally:
            job["running"] = False

    job.update(running=True, phase="iniciando", answer="", result="", started=time.time())
    job["thread"] = threading.Thread(target=_run, daemon=True, name="obsidian-maintenance")
    job["thread"].start()
    return {"running": True,
            "answer": "🧹 Manutenção iniciada" + (" (todas as notas)" if full else
                                                  " — só notas novas desde a última")}


def maintenance_status() -> dict:
    import time
    job = _maint_job
    t = job.get("thread")
    if job.get("running") and t is not None and not t.is_alive():
        job["running"] = False
    if job.get("running"):
        mins = int((time.time() - job.get("started", time.time())) // 60)
        text = f"_{job.get('phase', '')} — {mins} min_"
        if job.get("total"):
            text = (f"Notas analisadas: **{job.get('processed', 0)}/{job['total']}** · "
                    f"movidas: **{job.get('moved', 0)}**\n\n") + text
        return {"running": True, "result": "", "text": text}
    return {"running": False, "result": job.get("result", ""), "text": job.get("answer", "")}


def rebuild_indices(vault: Optional[str] = None) -> dict:
    """Regenera SÓ os índices MOC do Obsidian (00-meta/indices/) a partir do DISCO
    (fonte da verdade). Não depende do ChromaDB."""
    vaults_to_run = [vault] if vault else get_vaults()
    results = []
    for v in vaults_to_run:
        try:
            n_notes, n_subs = _build_vault_indices(v)
            results.append(f"[{v}] {n_notes} nota(s) em {n_subs} sub-tema(s) — índices em 00-meta/indices/")
            state.record_event(v, actor="system", kind="indices",
                               summary="Atualizou os índices do Obsidian (00-meta/indices)", source="obsidian")
        except Exception as e:
            log.error(f"[indices] erro em {v}: {e}")
            results.append(f"[{v}] erro: {e}")
    return {
        "answer": "✅ Índices do Obsidian atualizados:\n" + "\n".join(f"- {r}" for r in results),
        "sources": [],
    }


def _disk_notes(vault: str) -> list:
    """Notas do vault lidas do DISCO (fonte da verdade): id (minúsculo), link (caminho
    real, sem .md, para wiki links), título e data do frontmatter. Exclui 00-meta."""
    from core.text import RELATED_SECTION_RE
    base = vault_fs.vault_dir(vault)
    out = []
    for nid, p in vault_fs.iter_notes(vault):
        rel = p.relative_to(base).as_posix()
        try:
            content = vault_fs.read_path(p)
            fm = _parse_frontmatter(content)
        except Exception:
            content, fm = "", {}
        try:
            mtime = p.stat().st_mtime
        except Exception:
            mtime = 0.0
        sec = RELATED_SECTION_RE.search(content)
        related = re.findall(r"\[\[([^\]|#]+)", sec.group(0)) if sec else []
        stem = re.sub(r"^\d{4}-\d{2}-\d{2}-?", "", p.stem) or p.stem
        title = str(fm.get("title") or stem.replace("-", " ").strip().capitalize())
        out.append({"id": nid, "link": rel[:-3] if rel.endswith(".md") else rel,
                    "title": title.replace("|", "-").replace("]", ")").replace("[", "("),
                    "date": str(fm.get("date") or ""), "mtime": mtime,
                    "related": [r.strip() for r in related]})
    return out


INDEX_SUBS_DIR = "00-meta/indices/sub-temas"


def _build_vault_indices(vault: str, col=None) -> tuple:
    """Gera a hierarquia de índices (MOC) em 00-meta/indices/ a partir do DISCO. Zero LLM.
    Graph view: _mapa-geral → sub-temas (pasta de cada nota) → notas.
    Apaga sub-temas que não existem mais. Retorna (nº de notas, nº de sub-temas)."""
    import datetime
    from collections import defaultdict
    today = datetime.date.today().isoformat()

    notes = _disk_notes(vault)
    by_sub: dict = defaultdict(list)
    roots = set()
    for n in notes:
        parts = n["link"].split("/")
        if len(parts) >= 2:
            roots.add(parts[0])
            sub = parts[-2]
        else:
            sub = "sem-categoria"
        by_sub[sub].append(n)

    # Limpa índices antigos: por pasta raiz (formato anterior) e sub-temas sem notas.
    for root in roots:
        try:
            vault_fs.delete_note(vault, f"00-meta/indices/{root}.md")
        except Exception:
            pass
    # (list_notes ignora 00-meta de propósito, então lista a pasta direto.)
    wanted = {f"{sub}.md".lower() for sub in by_sub}
    subs_dir = vault_fs.vault_dir(vault) / INDEX_SUBS_DIR
    if subs_dir.is_dir():
        for f in subs_dir.glob("*.md"):
            if f.name.lower() not in wanted:
                try:
                    f.unlink()
                    log.info(f"[indices] removido sub-tema sem notas: {f.name}")
                except Exception:
                    pass

    def _write_if_changed(path: str, content: str) -> None:
        # Ignora a linha de data ao comparar: evita reescrever o arquivo todo dia.
        strip = lambda t: re.sub(r"(?m)^date: .*$|gerado automaticamente em .*$", "", t)
        try:
            if strip(vault_fs.read_note(vault, path)) == strip(content):
                return
        except Exception:
            pass
        vault_fs.write_note(vault, path, content)

    for sub, items in sorted(by_sub.items()):
        lines = [
            "---", f"title: Sub-tema — {sub}", f"date: {today}", "tags:",
            "  - indice", "  - sub-tema", "  - auto-gerado", "---", "",
            f"# Sub-tema: {sub}", "", f"> {len(items)} nota(s) · gerado automaticamente", "",
        ]
        for n in sorted(items, key=lambda x: x["link"].lower()):
            lines.append(f"- [[{n['link']}|{n['title']}]]")
        _write_if_changed(f"{INDEX_SUBS_DIR}/{sub}.md", "\n".join(lines) + "\n")
    log.info(f"[indices] {len(by_sub)} índice(s) de sub-tema em {INDEX_SUBS_DIR}/")

    map_lines = [
        "---", "title: Mapa Geral do Vault", f"date: {today}", "tags:",
        "  - indice", "  - mapa", "  - auto-gerado", "---", "",
        f"# Mapa Geral — {vault}", "",
        f"> {len(notes)} nota(s) · {len(by_sub)} sub-tema(s) · gerado automaticamente em {today}",
        "", "## Sub-temas",
    ]
    for sub in sorted(by_sub, key=str.lower):
        map_lines.append(f"- [[{INDEX_SUBS_DIR}/{sub}|{sub}]] ({len(by_sub[sub])})")
    _write_if_changed("00-meta/indices/_mapa-geral.md", "\n".join(map_lines) + "\n")
    log.info(f"[indices] _mapa-geral.md → {len(by_sub)} sub-temas, {len(notes)} notas")
    return len(notes), len(by_sub)


def _is_settings_note(note_id: str) -> bool:
    """Retorna True se a nota pertence à pasta 00-meta (índices/config do agente)."""
    return is_meta(note_id)


def tool_search_vault(query: str, collections: Optional[list] = None, db: str = None,
                      tags: Optional[str] = None, date_from: Optional[str] = None) -> str:
    """Busca semântica no vault (uma coleção por vault). Suporta filtro por tags e data."""
    vault  = _vault(db)
    col_nm = _col_name(vault)
    chroma = get_chroma()

    try:
        col = chroma.get_collection(col_nm)
    except Exception:
        return "Vault ainda não indexado pelo watcher."

    embedding = embedder.embed_one(query)

    # Filtros de metadata opcionais
    where: dict = {}
    if tags:
        where["tags"] = {"$contains": tags}
    if date_from:
        where["date"] = {"$gte": date_from}

    try:
        r = col.query(
            query_embeddings=[embedding],
            n_results=12,
            where=where if where else None,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        return f"Erro na busca: {e}"

    seen, results = set(), []
    for doc, meta, dist in zip(r["documents"][0], r["metadatas"][0], r["distances"][0]):
        nid = meta.get("note_id", "")
        if not nid or _is_settings_note(nid) or nid in seen:
            continue
        seen.add(nid)
        results.append({
            "note_id": nid,
            "score":   round(1 - dist, 3),
            "excerpt": doc[:400],
            "title":   meta.get("title", ""),
            "tags":    meta.get("tags", ""),
            "date":    meta.get("date", ""),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    if not results:
        return "Nenhuma nota encontrada."
    return json.dumps(results[:8], ensure_ascii=False)


def _get_link_graph(db: str = None) -> dict:
    """Lê o grafo de links do SQLite (src → [dst])."""
    return state.link_graph(_vault(db))


def tool_get_neighbors(note_id: str, db: str = None) -> str:
    """Retorna notas diretamente conectadas via wiki links (grafo de conhecimento).
    outgoing = notas que esta nota menciona; incoming = notas que mencionam esta."""
    graph    = _get_link_graph(db=db)
    outgoing = graph.get(note_id, [])
    incoming = [nid for nid, links in graph.items() if note_id in links]
    return json.dumps({
        "note_id":       note_id,
        "outgoing_links": outgoing,
        "incoming_links": incoming,
        "degree":        len(set(outgoing + incoming)),
    }, ensure_ascii=False)


def tool_graph_search(start_note_id: str, depth: int = 2, db: str = None) -> str:
    """BFS no grafo de links a partir de uma nota — mapeia a rede de conhecimento conectada.
    depth=1 vizinhos diretos, depth=2 vizinhos de vizinhos, etc."""
    graph   = _get_link_graph(db=db)
    visited: dict[str, dict] = {}
    queue   = [(start_note_id, 0)]

    while queue:
        node, d = queue.pop(0)
        if node in visited or d > depth:
            continue
        outgoing = graph.get(node, [])
        incoming = [nid for nid, links in graph.items() if node in links]
        visited[node] = {"depth": d, "outgoing": outgoing, "incoming": incoming}
        for neighbor in set(outgoing + incoming):
            if neighbor not in visited:
                queue.append((neighbor, d + 1))

    return json.dumps({
        "start":        start_note_id,
        "depth":        depth,
        "nodes_found":  len(visited),
        "network":      visited,
    }, ensure_ascii=False)


def tool_read_note(note_id: str, db: str = None) -> str:
    """Lê o conteúdo completo de uma nota do disco."""
    v = _vault(db)
    try:
        if not vault_fs.exists(v, note_id):
            return f"Erro ao ler nota '{note_id}': não encontrada."
        content = vault_fs.read_note(v, note_id)
        return content if content else "(nota vazia)"
    except Exception as e:
        return f"Erro ao ler nota '{note_id}': {e}"


def tool_edit_note(note_id: str, content: str, db: str = None) -> str:
    """Edita (sobrescreve) o conteúdo de uma nota existente no disco."""
    v = _vault(db)
    try:
        if not vault_fs.exists(v, note_id):
            return f"Nota '{note_id}' não existe."
        vault_fs.write_note(v, note_id, content)
        return f"Nota '{note_id}' atualizada com sucesso."
    except Exception as e:
        return f"Erro ao editar nota '{note_id}': {e}"


def tool_list_notes(db: str = None) -> str:
    """Lista todas as notas do vault (exclui 00-meta)."""
    try:
        return json.dumps(vault_fs.list_notes(_vault(db)), ensure_ascii=False)
    except Exception as e:
        return f"Erro ao listar notas: {e}"


def tool_create_note(path: str, content: str, db: str = None) -> str:
    """Cria (ou sobrescreve) uma nota no vault. path ex: '00-inbox/minha-nota.md'"""
    v = _vault(db)
    try:
        nid = vault_fs.write_note(v, path, content)
        return f"Nota '{nid}' criada com sucesso."
    except Exception as e:
        return f"Erro ao criar nota: {e}"


# ── Ingestão de arquivos e URLs ──────────────────────────────────────────────
def _extract_text_from_file(filename: str, content: bytes) -> str:
    """Extrai texto de PDF, DOCX, TXT, MD ou HTML."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if ext in ("docx",):
        from docx import Document
        doc = Document(io.BytesIO(content))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if ext in ("html", "htm"):
        from bs4 import BeautifulSoup
        import markdownify
        soup = BeautifulSoup(content, "html.parser")
        return markdownify.markdownify(str(soup), heading_style="ATX")
    # txt, md, csv, json — trata como texto plano
    return content.decode("utf-8", errors="replace")


def _extract_text_from_url(url: str) -> str:
    """Busca URL e extrai texto em markdown."""
    import markdownify
    from bs4 import BeautifulSoup
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.com/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    session = requests.Session()
    r = session.get(url, timeout=30, headers=headers, allow_redirects=True)
    if r.status_code == 403:
        raise Exception(
            f"403 Forbidden — o site bloqueou o acesso automático. "
            f"Copie o texto da página e importe como arquivo .txt."
        )
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if "pdf" in ct:
        return _extract_text_from_file("page.pdf", r.content)
    soup = BeautifulSoup(r.content, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    return markdownify.markdownify(str(main), heading_style="ATX")


INGEST_PROMPT = """Você é o Mordomo do Conhecimento. Converta o conteúdo abaixo em uma nota Obsidian bem estruturada.

{load_rules_section}

REGRAS GERAIS (aplicar somente se não houver REGRAS DO VAULT acima):
- Estrutura: SEMPRE no máximo 2 níveis — knowledge/{categoria}/{arquivo}.md.
  NUNCA crie subpastas dentro de uma categoria (proibido knowledge/cat/subcat/arquivo.md).
- Slug do ARQUIVO (nome .md) sempre em lowercase com hifens e sem acentos, seja qual for a língua.
- Frontmatter obrigatório: title, date ({today}), source, tags (3-5). As CHAVES ficam em inglês;
  os VALORES (título, tags) seguem a língua do conteúdo original.
- Seções com ## e ###, conciso e navegável.
- LÍNGUA: escreva o corpo da nota NA MESMA LÍNGUA do conteúdo original — inglês→inglês,
  português→pt-BR, espanhol→espanhol, etc. NÃO traduza.
- Wiki links com alias: [[caminho/nota|Nome Visível]]

NOTAS EXISTENTES NO VAULT:
{note_ids}

AÇÕES / TO-DOS:
- Extraia as tarefas acionáveis para MIM = o LEITOR indicado no topo do conteúdo (quando houver):
  algo que EU preciso fazer, responder, revisar, enviar, aprovar ou decidir.
- Pedidos dirigidos a mim contam, mesmo educados ou em outra língua: "could you please share X",
  "kindly review and share your feedback", "please provide reasons", "pode validar?".
  Se o email pede algo a mim e a outras pessoas juntas, a tarefa também é minha.
- Ex.: "Responder Fulano sobre X", "Enviar a base Y completa", "Revisar e validar o arquivo Z".
- NÃO invente tarefas; se o conteúdo é só informativo (newsletter, comunicado, confirmação), deixe a lista vazia.
- Cada tarefa: objetiva, com prazo se houver (YYYY-MM-DD), NA MESMA LÍNGUA do conteúdo original.

Responda APENAS com um JSON válido (sem texto antes ou depois):
{{"path": "knowledge/categoria/aaaa-mm-dd-nome.md", "content": "conteúdo completo da nota em markdown", "summary": "1-2 frases descrevendo o que foi criado", "todos": [{{"title": "ação objetiva", "due": "YYYY-MM-DD ou vazio"}}]}}"""


def _find_related_for_new_note(content: str, exclude_path: str, db: str = None, threshold: float = 0.72) -> list:
    """Busca notas semanticamente relacionadas para auto-linking no ingest."""
    vault = _vault(db)
    try:
        col = get_chroma().get_collection(_col_name(vault))
        embedding = embedder.embed_one(content[:1000])
        r = col.query(query_embeddings=[embedding], n_results=10, include=["metadatas", "distances"])
        related, seen = [], {exclude_path.lower()}
        for meta, dist in zip(r["metadatas"][0], r["distances"][0]):
            nid = meta.get("note_id", "")
            score = round(1 - dist, 3)
            if nid and nid not in seen and score >= threshold and not _is_settings_note(nid):
                related.append({"note_id": nid, "title": meta.get("title") or nid.split("/")[-1].replace(".md", ""), "score": score})
                seen.add(nid)
        return related[:5]
    except Exception:
        return []


def _graph_enrich_search(args: dict, db: str = None) -> str:
    """Chama tool_search_vault e enriquece cada resultado com notas conectadas no grafo."""
    raw = tool_search_vault(db=db, **args)
    try:
        results = json.loads(raw)
        graph = _get_link_graph(db=db)
        for item in results:
            nid = item.get("note_id", "")
            if not nid:
                continue
            out = graph.get(nid, [])
            inc = [k for k, v in graph.items() if nid in v]
            connected = list(dict.fromkeys(out + inc))[:6]
            if connected:
                item["connected_notes"] = connected
        return json.dumps(results, ensure_ascii=False)
    except Exception:
        return raw


def _parse_llm_json(raw: str) -> Optional[dict]:
    """Extrai o objeto JSON da resposta do LLM, tolerando o que ele costuma fazer:
    quebras de linha/tabulações cruas dentro das strings (comum ao reproduzir tabelas
    — o json.loads padrão rejeita), cerca ```json em volta e texto antes/depois.
    Não mexe em blocos de código que existam DENTRO da nota."""
    t = (raw or "").strip()
    m = re.match(r"^```(?:json)?[ \t]*\n(.*)\n```\s*$", t, re.DOTALL)
    if m:
        t = m.group(1)
    candidates = [t]
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        candidates.append(t[i:j + 1])
    for c in candidates:
        try:
            d = json.loads(c, strict=False)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return None


def ingest(raw_text: str, source_name: str, vault: Optional[str] = None, db: str = None,
           todo_priority: str = "media", todo_source: str = "email", todo_link: str = "") -> dict:
    """Converte texto bruto em nota Obsidian e salva no vault.
    todo_priority: prioridade aplicada aos to-dos extraídos (alta/media/baixa)."""
    import datetime
    db = db or vault  # vault name == subdiretório em disco
    note_ids = tool_list_notes(db=db)
    today = datetime.date.today().isoformat()

    # Carrega regras do vault — têm prioridade sobre as regras gerais
    load_rules_section = ""
    if vault:
        settings = load_settings(vault, db=db)
        if settings.get("load"):
            load_rules_section = (
                f"⚠️ REGRAS DO VAULT (PRIORIDADE MÁXIMA — seguir à risca, ignorar qualquer exemplo genérico abaixo):\n"
                f"{settings['load']}"
            )

    vault_ctx = f"\nVAULT: {vault}" if vault else ""
    prompt = (
        INGEST_PROMPT
        .replace("{load_rules_section}", load_rules_section)
        .replace("{note_ids}", note_ids[:3000])
        .replace("{today}", today)
    ) + vault_ctx
    user_msg = f"FONTE: {source_name}\n\nCONTEÚDO:\n{raw_text[:8000]}"

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user",   "content": user_msg},
    ]

    for attempt in range(3):
        raw = _call_llm_raw(messages)
        data = _parse_llm_json(raw)
        if data is None:
            log.warning(f"[ingest] resposta do LLM não é JSON válido (tentativa {attempt + 1}/3)")
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "Responda APENAS com um JSON válido conforme o formato solicitado."})
            continue

        path    = data.get("path", "00-inbox/imported.md")
        content = data.get("content", raw_text[:4000])
        summary = data.get("summary", f"Nota criada a partir de {source_name}")

        # Organizador: se houver instructions.md, o caminho é decidido pela taxonomia
        # (ChromaDB roteia, LLM desempata) — ignora o path livre do LLM.
        try:
            import organizer
            fm_data = _parse_frontmatter(content)
            routed = organizer.classify_note(
                db, content, title=fm_data.get("title") or summary[:60],
                summary=summary, date=fm_data.get("date") or today,
            )
            if routed and routed.get("path"):
                path = routed["path"]
        except Exception as e:
            log.warning(f"organizer (ingest): {e}")

        # Guarda final de profundidade: mesmo que o organizador falhe, nunca deixa
        # o path livre do LLM criar subpastas (knowledge/cat/subcat/arquivo.md).
        try:
            import organizer
            path = organizer.enforce_structure(db, path)
        except Exception as e:
            log.warning(f"enforce_structure: {e}")

        # Auto-link: busca notas relacionadas e adiciona seção antes de salvar
        related = _find_related_for_new_note(content, path, db=db)
        if related and "## Notas Relacionadas" not in content:
            links = "\n".join(
                f"- [[{r['note_id']}|{r['title']}]]  (similaridade: {r['score']})"
                for r in related
            )
            content += f"\n\n## Notas Relacionadas\n{links}"

        result = tool_create_note(path, content, db=db)
        log.info(f"Ingest: {result}")
        state.record_event(_vault(db), actor="agent", kind="ingest",
                           summary=f"Importou '{source_name}' → {path}", source="ingest", ref=path)

        # To-dos extraídos da destilação → tabela de to-dos (alimenta o tile)
        todos_created = 0
        try:
            for td in (data.get("todos") or [])[:10]:
                if isinstance(td, str):
                    title, due = td, ""
                else:
                    title, due = td.get("title", ""), td.get("due", "")
                if title and title.strip():
                    tid = state.add_todo(_vault(db), title, due=due, source=todo_source,
                                         ref=path, note_path=path, priority=todo_priority,
                                         link=todo_link)
                    if tid:
                        todos_created += 1
                        state.record_event(_vault(db), actor="agent", kind="todo",
                                           summary=f"[{todo_priority}] {title}"[:200], source=todo_source, ref=path)
        except Exception as e:
            log.warning(f"ingest todos: {e}")

        return {
            "answer":  f"{summary}\n\nNota criada: `{path}`\n\n{result}",
            "sources": [path],
            "path":    path,
            "todos_created": todos_created,
        }

    return {"answer": "Falha ao processar o conteúdo após 3 tentativas.", "sources": [], "failed": True}


def _inject_frontmatter_keys(vault: str, note_id: str, keys: dict) -> None:
    """Garante no frontmatter da nota os pares informados (ponteiro p/ a fonte).
    Não duplica chaves já presentes."""
    try:
        content = vault_fs.read_note(vault, note_id)
    except Exception:
        return
    add_lines = [f"{k}: {v}" for k, v in keys.items() if v]
    if not add_lines:
        return
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            fm = content[3:end]
            add = "\n".join(l for l in add_lines if not re.search(rf"(?m)^{re.escape(l.split(':')[0])}:", fm))
            new = "---" + fm + (("\n" + add) if add else "") + content[end:]
            vault_fs.write_note(vault, note_id, new)
            return
    fm_block = "---\n" + "\n".join(add_lines) + "\n---\n"
    vault_fs.write_note(vault, note_id, fm_block + content)


class EmailIngestFailed(RuntimeError):
    """A destilação do email falhou (ex.: resposta do LLM ilegível nas 3 tentativas)."""


def ingest_email(subject: str, sender: str, date: str, body: str,
                 msg_id: str = "", web_link: str = "", vault: Optional[str] = None,
                 db: str = None, to: str = "", cc: str = "", me: str = "", from_email: str = "", to_emails: str = "", cc_emails: str = "", me_email: str = "") -> dict:
    """Destila um email em nota do vault (conhecimento destilado + ponteiro p/ a fonte).
    Reaproveita o pipeline de ingestão (LLM distila → nota → auto-link → watcher indexa)."""
    # Enfileira para processamento assíncrono (resposta rápida; sem timeout de proxy).
    if msg_id and state.is_processed("outlook", msg_id):
        return {"queued": False, "skipped": True, "answer": "(email já processado)"}
    payload = {"subject": subject, "sender": sender, "date": date, "body": body,
               "msg_id": msg_id, "web_link": web_link, "vault": vault or db,
               "to": to, "cc": cc, "me": me, "from_email": from_email,
               "to_emails": to_emails, "cc_emails": cc_emails, "me_email": me_email}
    state.enqueue("outlook", msg_id, payload)
    if msg_id:
        state.mark_processed("outlook", msg_id, item_date=date, status="pending")
    return {"queued": True, "answer": "enfileirado"}


def _split_emails(s: str) -> set:
    return {e.strip().lower() for e in re.split(r"[;,\s]+", s or "") if "@" in e}


def _my_names(me: str) -> tuple:
    """(como veio, primeiro nome, 'Nome Sobrenome') em minúsculas. Entende 'Sobrenome, Nome'."""
    me = (me or "").strip()
    if "," in me:
        last, first = [x.strip() for x in me.split(",", 1)]
        first = first.split()[0] if first else ""
        return me.lower(), first.lower(), f"{first} {last}".strip().lower()
    first = me.split()[0] if me else ""
    return me.lower(), first.lower(), me.lower()


def _has_word(text: str, word: str) -> bool:
    return bool(word) and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


def _email_priority(me: str, to: str, cc: str, subject: str, body: str,
                    me_email: str = "", to_emails: str = "", cc_emails: str = "") -> str:
    """Prioridade determinística (sem LLM) a partir do papel do usuário no email:
    no Para → alta ; só em Cópia → media (alta se citado pelo nome) ; citado → media ;
    nada disso (ex.: lista de distribuição) → baixa.
    Com endereços SMTP (collector novo) a checagem de Para/Cc é exata; sem eles,
    cai para o nome de exibição."""
    raw, first, full = _my_names(me)
    text = f"{subject} {body}".lower()
    mentioned = _has_word(text, first)
    me_email = (me_email or "").strip().lower()
    if me_email and (to_emails or cc_emails):
        in_to = me_email in _split_emails(to_emails)
        in_cc = me_email in _split_emails(cc_emails)
    else:
        if not raw:
            return "media"
        to_l, cc_l = (to or "").lower(), (cc or "").lower()
        in_to = (raw in to_l) or (full in to_l)
        in_cc = (raw in cc_l) or (full in cc_l)
    if in_to:
        return "alta"
    if in_cc:
        return "alta" if mentioned else "media"
    if mentioned:
        return "media"
    return "baixa"


# Remetentes que nunca merecem rascunho automático (ruído transacional/broadcast).
_NOREPLY_RE   = re.compile(r"(?i)(no.?reply|do.?not.?reply|n[aã]o.?responda|noreply|donotreply|"
                           r"postmaster|mailer-?daemon|notifica[cç]|notification|newsletter|automat|"
                           r"auto-?confirm)")
_BROADCAST_RE = re.compile(r"(?i)(internal.?comm|comunica[cç][aã]o interna|global comms|global comm|"
                           r"announce|broadcast|comunicados?\b|all.?staff|all.?employees)")
_AUTOSUBJ_RE  = re.compile(r"(?i)(automatic reply|out of office|auto.?reply|resposta autom[aá]tica|"
                           r"fora do escrit[oó]rio|aus[eê]ncia|on vacation|f[eé]rias)")


def _autodraft_block_reason(me: str, sender: str, to: str, cc: str, subject: str, body: str,
                            from_email: str = "", to_emails: str = "", cc_emails: str = "") -> str:
    """Motivo para NÃO gerar rascunho automático ('' = pode gerar). A INGESTÃO não é
    afetada — só o rascunho. Barra: lista 'never_draft' do usuário (nome ou endereço
    do remetente/destinatários), no-reply/notificações, comunicados/broadcast,
    auto-replies e email de massa."""
    try:
        from core.settings import get_settings
        never = get_settings().get("never_draft", []) or []
    except Exception:
        never = []
    hay = f"{sender} {from_email} {to} {to_emails} {cc} {cc_emails}".lower()
    hit = next((p for p in never if p and p in hay), None)
    if hit:
        return f"lista 'nunca gerar rascunho' ({hit})"
    s = f"{sender} {from_email}"
    if _NOREPLY_RE.search(s):
        return "remetente no-reply/notificação"
    if _BROADCAST_RE.search(s):
        return "remetente de comunicado"
    if _AUTOSUBJ_RE.search(subject or ""):
        return "resposta automática"
    # Email de massa (comunicado/lista de distribuição): muitos destinatários visíveis.
    n = max(len([x for x in re.split(r"[;,]", f"{to};{cc}") if x.strip()]),
            len(_split_emails(to_emails) | _split_emails(cc_emails)))
    if n >= 8:
        return f"email de massa ({n} destinatários)"
    return ""


def _should_autodraft(*args, **kwargs) -> bool:
    return not _autodraft_block_reason(*args, **kwargs)


DRAFT_SYSTEM = (
    "Você redige um RASCUNHO de resposta de email em nome do usuário (o Mordomo do "
    "Conhecimento). Escreva NA MESMA LÍNGUA do email original (inglês→inglês, "
    "português→pt-BR, espanhol→espanhol); não traduza. Tom profissional e objetivo, pronto "
    "para o usuário revisar e enviar. Baseie-se no email completo (quando fornecido), na nota "
    "destilada e no contexto do vault. NÃO invente fatos. Se faltar informação para responder, "
    "deixe um marcador claro entre colchetes, como [confirmar X] (na língua do email).\n\n"
    "FORMATO — é o texto que será colado direto no Outlook, então é TEXTO PURO de email, "
    "NÃO Markdown:\n"
    "- Proibido: **negrito**, __sublinhado__, # títulos, `crases`, tabelas, links [texto](url).\n"
    "- Parágrafos curtos, separados por uma linha em branco.\n"
    "- Listas: cada item numa linha começando com \"• \". Itens numerados: \"1. \", \"2. \"…\n"
    "- Para destacar o tema de um item, escreva-o no início seguido de dois-pontos "
    "(ex.: \"1. Taxa de câmbio: …\"), sem negrito.\n"
    "- Termine com a despedida e o nome do usuário informado no pedido.\n"
    "Responda SOMENTE com o corpo do email (sem assunto, sem JSON, sem comentários)."
)


def _email_plaintext(text: str) -> str:
    """Rede de segurança: tira a sintaxe de Markdown que escapar do LLM, para o rascunho
    colar limpo no Outlook (negrito/itálico, títulos, crases, links e bullets '-'/'*')."""
    t = (text or "").replace("\r\n", "\n").strip()
    t = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", t)                     # cerca de código em volta
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", t)   # *itálico*
    t = re.sub(r"`([^`\n]+)`", r"\1", t)
    t = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", t)   # [texto](url)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", t)                        # # títulos
    t = re.sub(r"(?m)^(\s*)[-*+]\s+", lambda m: m.group(1) + "• ", t)   # - item → • item
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _user_display_name() -> str:
    """Nome do usuário para assinar o rascunho ('Sobrenome, Nome' → 'Nome Sobrenome')."""
    raw = state.kv_get("*", "me_name") or ""
    return _my_names(raw)[2].title() if raw else ""


def process_email_payload(subject: str = "", sender: str = "", date: str = "", body: str = "",
                          msg_id: str = "", web_link: str = "", vault: Optional[str] = None,
                          db: str = None, to: str = "", cc: str = "", me: str = "", from_email: str = "",
                          to_emails: str = "", cc_emails: str = "", me_email: str = "") -> dict:
    """Destila de fato um email (chamado pelo worker): nota no vault + to-dos.
    Sem prioridade automática (quem prioriza é o usuário, na tela) e sem rascunho
    automático (o rascunho é pedido no tile Email). O corpo do email não é gravado.
    Pode levantar LLMRateLimited, ou EmailIngestFailed se a destilação falhar (o worker
    tenta de novo e, se não der, avisa com um to-do — um email nunca some em silêncio)."""
    db = db or vault
    if me and state.kv_get("*", "me_name") != me:
        state.kv_set("*", "me_name", me)        # para assinar os rascunhos
    # Papel do leitor no email (só para orientar o LLM e o to-do "Responder").
    role_level = _email_priority(me, to, cc, subject, body,
                                 me_email=me_email, to_emails=to_emails, cc_emails=cc_emails)
    role = {"alta": "destinatário direto (Para) ou citado — o email é PARA você",
            "media": "em cópia (Cc) ou citado", "baixa": "não é destinatário direto"}[role_level]
    who = (me or "").strip() + (f" <{me_email}>" if me_email else "")
    raw = (
        f"LEITOR (= \"MIM\" nas regras de to-dos): {who or '(não informado)'} — papel: {role}\n"
        f"Assunto: {subject}\n"
        f"De: {sender}" + (f" <{from_email}>" if from_email else "") + "\n"
        f"Para: {to}\n"
        f"Cc: {cc}\n"
        f"Data: {date}\n\n"
        f"{body}"
    )
    res = ingest(raw, source_name=f"email: {subject or '(sem assunto)'}", vault=vault, db=db,
                 todo_priority="", todo_source="email", todo_link=web_link)
    path = res.get("path")
    if not path:
        raise EmailIngestFailed(res.get("answer") or "destilação falhou")
    v = _vault(db)

    # Email dirigido a você (Para/citado), pessoal (não comunicado/notificação) e sem
    # nenhuma tarefa extraída → ao menos "Responder".
    personal = role_level == "alta" and not _autodraft_block_reason(
        me, sender, to, cc, subject, body, from_email, to_emails, cc_emails)
    if personal and not res.get("todos_created"):
        name = re.sub(r"\s*[-–(<].*$", "", sender or "").strip() or "remetente"
        tid = state.add_todo(v, f"Responder {name}: {subject}"[:300], source="email",
                             ref=path, note_path=path, link=web_link)
        if tid:
            state.record_event(v, actor="agent", kind="todo",
                               summary=f"Responder {name}: {subject}"[:200], source="email", ref=path)

    _inject_frontmatter_keys(v, path, {
        "source":     "outlook",
        "msg_id":     msg_id,
        "from":       sender,
        "from_email": from_email,
        "email_date": date,
        "web_link":   web_link,
    })
    state.upsert_email(v, msg_id or path, subject=subject, sender=sender, from_email=from_email,
                       email_date=date, note_path=path, web_link=web_link)
    state.record_event(v, actor="agent", kind="email", summary=f"Email de {sender}: {subject}"[:200],
                       source="outlook", ref=msg_id or path)
    return {"path": path, "answer": res.get("answer", ""), "sources": res.get("sources", [])}


def backfill_emails(vault: str, days: int = 60) -> int:
    """Uma vez por vault: preenche a lista do tile Email com os emails já processados
    (assunto/remetente do histórico de eventos; link e endereço do frontmatter da nota)."""
    import sqlite3
    import time
    v = _vault(vault)
    if state.kv_get(v, "emails_backfill_v1"):
        return 0
    since = time.time() - days * 86400
    n = 0
    with sqlite3.connect(state.STATE_DB) as c:
        c.row_factory = sqlite3.Row
        evs = c.execute("SELECT ref, summary FROM events WHERE vault=? AND kind='email' AND ts>=? "
                        "ORDER BY ts", (v, since)).fetchall()
        notes = {r["item_id"]: (r["note_path"], r["item_date"]) for r in c.execute(
            "SELECT item_id, note_path, item_date FROM processed_items WHERE source='outlook' AND status='ok'")}
    for e in evs:
        msg_id = e["ref"] or ""
        m = re.match(r"^(?:\[\w+\]\s*)?Email de (.*?): (.*)$", e["summary"] or "")
        if not msg_id or not m or msg_id not in notes:
            continue
        note_path, item_date = notes[msg_id]
        fm = {}
        try:
            fm = _parse_frontmatter(vault_fs.read_note(v, note_path))
        except Exception:
            pass
        state.upsert_email(v, msg_id, subject=m.group(2), sender=m.group(1),
                           from_email=str(fm.get("from_email") or ""),
                           email_date=str(fm.get("email_date") or item_date or ""),
                           note_path=note_path, web_link=str(fm.get("web_link") or ""))
        n += 1
    state.kv_set(v, "emails_backfill_v1", n)
    log.info(f"[emails] lista preenchida com {n} email(s) do histórico")
    return n


# ── Rascunho sob demanda (botão no tile Email) ──────────────────────────────────
# O corpo completo vem do Outlook pelo collector (que roda no PC) e só passa pela
# memória: não é gravado no vault nem no banco.

def request_email_draft(vault: str, msg_id: str, from_note: bool = False) -> dict:
    """Pede um rascunho. Normal: marca 'waiting' e o collector traz o email inteiro.
    from_note=True: gera já, só com a nota destilada (ex.: PC desligado)."""
    v = _vault(vault)
    em = state.get_email(v, msg_id)
    if not em:
        return {"ok": False, "error": "email não encontrado"}
    state.discard_email_drafts(v, msg_id)
    # Sem EntryID nem Message-ID (com @), o collector não tem como achar o email.
    if from_note or not (em.get("entry_id") or "@" in msg_id):
        _start_draft_thread(v, em, full_email="")
        return {"ok": True, "status": "generating"}
    state.set_email_draft_status(v, msg_id, "waiting")
    return {"ok": True, "status": "waiting"}


def receive_email_body(vault: str, msg_id: str, found: bool, full_email: str = "", error: str = "") -> dict:
    """O collector entregou (ou não achou) o email pedido."""
    v = _vault(vault)
    em = state.get_email(v, msg_id)
    if not em or em.get("draft_status") != "waiting":
        return {"ok": False}
    if not found:
        state.set_email_draft_status(v, msg_id, "error",
                                     error or "o email não foi encontrado no Outlook (movido ou apagado?)")
        return {"ok": True}
    _start_draft_thread(v, em, full_email=full_email)
    return {"ok": True}


def _start_draft_thread(v: str, em: dict, full_email: str) -> None:
    import threading
    state.set_email_draft_status(v, em["msg_id"], "generating")

    def _run():
        try:
            generate_reply_draft(v, em.get("note_path", ""), em.get("subject", ""), em.get("sender", ""),
                                 ref=em["msg_id"], db=v, from_email=em.get("from_email", ""),
                                 full_email=full_email)
            state.set_email_draft_status(v, em["msg_id"], "ready")
        except sdk_llm.LLMRateLimited:
            state.set_email_draft_status(v, em["msg_id"], "error",
                                         "limite de uso do Claude atingido — tente mais tarde")
        except Exception as e:
            log.error(f"[draft] erro ao gerar rascunho: {e}")
            state.set_email_draft_status(v, em["msg_id"], "error", f"erro ao gerar: {e}"[:300])

    threading.Thread(target=_run, daemon=True, name="draft").start()


def generate_reply_draft(vault: str, note_path: str, subject: str, reply_to: str,
                         ref: str = "", priority: str = "", db: str = None,
                         from_email: str = "", full_email: str = "") -> dict:
    """Gera um rascunho de resposta: email completo (quando o collector trouxe) + nota
    destilada + contexto do vault. Pode levantar LLMRateLimited."""
    v = _vault(db or vault)
    try:
        note = tool_read_note(note_path, db=v) if note_path else ""
    except Exception:
        note = ""
    ctx = ""
    try:
        raw = tool_search_vault(subject or (note[:80]), db=v)
        results = json.loads(raw) if raw.startswith("[") else []
        ctx = "\n\n".join(f"- {r.get('title','')}: {r.get('excerpt','')[:300]}" for r in results[:4])
    except Exception:
        pass
    parts = []
    if full_email:
        parts.append(f"EMAIL COMPLETO (inclui o histórico da conversa):\n{full_email[:15000]}")
    parts.append(f"NOTA DESTILADA DESTE EMAIL:\n{note[:3000] or '(sem nota)'}")
    parts.append(f"CONTEXTO DO VAULT (pode ajudar):\n{ctx or '(sem contexto relevante)'}")
    me_name = _user_display_name()
    parts.append(f"Escreva o rascunho de resposta para {reply_to or 'o remetente'}, respondendo "
                 f"a TODOS os pontos e perguntas da mensagem mais recente. "
                 f"Assine como: {me_name or '[seu nome]'}.")
    body = _email_plaintext(_call_llm_raw([{"role": "system", "content": DRAFT_SYSTEM},
                                           {"role": "user", "content": "\n\n".join(parts)}]))
    subj = subject if (subject or "").lower().startswith("re:") else f"Re: {subject}"
    did = state.add_draft(v, source="email", ref=ref, subject=subj, body=body,
                          reply_to=reply_to, note_path=note_path, from_email=from_email)
    if did:
        state.record_event(v, actor="agent", kind="draft",
                           summary=f"Rascunho de resposta: {subj}"[:200], source="email", ref=ref)
    return {"id": did, "subject": subj, "body": body, "reply_to": reply_to}


def enqueue_event(subject: str = "", start: str = "", end: str = "", organizer: str = "",
                  location: str = "", attendees: str = "", body: str = "", event_id: str = "",
                  web_link: str = "", vault: Optional[str] = None, db: str = None) -> dict:
    """Enfileira uma reunião/compromisso do calendário para processamento assíncrono."""
    if event_id and state.is_processed("calendar", event_id):
        return {"queued": False, "skipped": True, "answer": "(reunião já processada)"}
    payload = {"subject": subject, "start": start, "end": end, "organizer": organizer,
               "location": location, "attendees": attendees, "body": body,
               "event_id": event_id, "web_link": web_link, "vault": vault or db}
    state.enqueue("calendar", event_id, payload)
    if event_id:
        state.mark_processed("calendar", event_id, item_date=(start or "")[:10], status="pending")
    return {"queued": True, "answer": "enfileirado"}


def process_event_payload(subject: str = "", start: str = "", end: str = "", organizer: str = "",
                          location: str = "", attendees: str = "", body: str = "", event_id: str = "",
                          web_link: str = "", vault: Optional[str] = None, db: str = None) -> dict:
    """Destila uma reunião/compromisso (chamado pelo worker). Pode levantar LLMRateLimited."""
    db = db or vault
    raw = (
        f"REUNIÃO: {subject}\n"
        f"Quando: {start} — {end}\n"
        f"Organizador: {organizer}\n"
        f"Local: {location}\n"
        f"Participantes: {attendees}\n\n"
        f"{body}"
    )
    res = ingest(raw, source_name=f"reunião: {subject or '(sem título)'}", vault=vault, db=db)
    path = res.get("path")
    if path:
        _inject_frontmatter_keys(_vault(db), path, {
            "source":       "calendar",
            "event_id":     event_id,
            "start":        start,
            "end":          end,
            "organizer":    organizer,
            "web_link":     web_link,
        })
        state.record_event(_vault(db), actor="agent", kind="meeting",
                           summary=f"Reunião: {subject} ({start})"[:200],
                           source="calendar", ref=event_id or path)
    return {"path": path, "answer": res.get("answer", ""), "sources": res.get("sources", [])}


def ingest_file(filename: str, content: bytes, vault: Optional[str] = None, db: str = None) -> dict:
    raw_text = _extract_text_from_file(filename, content)
    return ingest(raw_text, source_name=filename, vault=vault, db=db)


def ingest_url(url: str, vault: Optional[str] = None, db: str = None) -> dict:
    try:
        raw_text = _extract_text_from_url(url)
    except Exception as e:
        return {"answer": f"Erro ao buscar URL: {e}", "sources": []}
    return ingest(raw_text, source_name=url, vault=vault, db=db)


def ingest_zip(content: bytes, vault: Optional[str] = None, db: str = None) -> dict:
    """Extrai um ZIP e ingere cada arquivo suportado como nota Obsidian."""
    import zipfile
    SUPPORTED = {"txt", "md", "html", "htm", "pdf", "docx"}
    created, errors = [], []

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            # Filtra apenas arquivos suportados
            names = [n for n in names if (n.rsplit(".", 1)[-1].lower() if "." in n else "") in SUPPORTED]
            log.info(f"ZIP: {len(names)} arquivos suportados para ingestão")
            for name in names:
                try:
                    file_bytes = zf.read(name)
                    basename   = os.path.basename(name)
                    result     = ingest_file(basename, file_bytes, vault=vault, db=db)
                    created.append(result.get("path", basename))
                    log.info(f"ZIP ingest OK: {result.get('path')}")
                except Exception as e:
                    errors.append(f"{name}: {e}")
                    log.error(f"ZIP ingest erro {name}: {e}")
    except zipfile.BadZipFile:
        return {"answer": "Arquivo ZIP inválido ou corrompido.", "sources": []}

    lines = [f"**{len(created)} notas criadas** de {len(names)} arquivos no ZIP"]
    if vault:
        lines.append(f"Vault: `{vault}`")
    lines.append("")
    lines.extend(f"- `{p}`" for p in created)
    if errors:
        lines.append(f"\n**{len(errors)} erros:**")
        lines.extend(f"- {e}" for e in errors)

    return {"answer": "\n".join(lines), "sources": created}


# ── Tool calling loop ─────────────────────────────────────────────────────────
def tool_move_note(source_id: str, dest_id: str, db: str = None) -> str:
    """Move/renomeia uma nota no disco. O watcher atualiza o índice automaticamente."""
    v = _vault(db)
    source_id = source_id.lower().strip()
    dest_id   = dest_id.lower().strip()
    if source_id == dest_id:
        return f"Nota já está em '{dest_id}' — nenhuma ação necessária."
    if _is_settings_note(source_id):
        return f"Nota '{source_id}' é interna/protegida e não pode ser movida."
    try:
        if not vault_fs.exists(v, source_id):
            return f"Nota '{source_id}' não existe."
        nid = vault_fs.move_note(v, source_id, dest_id)
        # Atualiza o índice imediatamente (watcher também pega, mas evitamos janela).
        try:
            indexer.remove_note(v, source_id if source_id.endswith(".md") else source_id + ".md")
            indexer.index_note(v, nid, vault_fs.read_note(v, nid), force=True)
        except Exception:
            pass
        log.info(f"move_note: {source_id} → {nid}")
        return f"Nota movida: '{source_id}' → '{nid}'"
    except Exception as e:
        return f"Erro ao mover nota: {e}"


def tool_delete_note(note_id: str, db: str = None) -> str:
    """Apaga uma nota do disco. O watcher remove do índice automaticamente."""
    v = _vault(db)
    try:
        if not vault_fs.delete_note(v, note_id):
            return f"Nota '{note_id}' já não existia."
        nid = note_id.lower() if note_id.endswith(".md") else note_id.lower() + ".md"
        try:
            indexer.remove_note(v, nid)
        except Exception:
            pass
        log.info(f"delete_note: {note_id}")
        return f"Nota '{note_id}' deletada."
    except Exception as e:
        return f"Erro ao deletar nota '{note_id}': {e}"


def tool_add_tags(note_id: str, tags: list, db: str = None) -> str:
    """Adiciona tags ao frontmatter YAML de uma nota. Cria o bloco --- se não existir. Não duplica tags já presentes."""
    v = _vault(db)
    try:
        if not vault_fs.exists(v, note_id):
            return f"Nota '{note_id}' não existe."
        content = vault_fs.read_note(v, note_id)

        # Extrai frontmatter
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end == -1:
                # Sem fechamento — adiciona frontmatter do zero antes do conteúdo
                existing_tags = []
                fm = ""
                body = content
            else:
                fm = content[3:end]
                body = content[end + 4:]  # após o ---\n de fechamento
                # Extrai tags já existentes
                existing_tags = re.findall(r"^\s*-\s+(\S+)", fm.split("tags:", 1)[-1].split("\n\n")[0], re.MULTILINE) if "tags:" in fm else []
        else:
            fm = ""
            body = content
            existing_tags = []

        new_tags = [t for t in tags if t not in existing_tags]
        if not new_tags:
            return f"Nenhuma tag nova para adicionar em '{note_id}' (já existem: {existing_tags})."

        tags_yaml = "\n".join(f"  - {t}" for t in new_tags)

        if fm and "tags:" in fm:
            # Insere novas tags logo após o último item existente da lista
            updated_fm = re.sub(
                r"(tags:(?:\n  - \S+)*)",
                lambda m: m.group(0) + "\n" + tags_yaml,
                fm,
            )
        elif fm:
            updated_fm = fm.rstrip() + f"\ntags:\n{tags_yaml}\n"
        else:
            updated_fm = f"tags:\n{tags_yaml}\n"

        new_content = f"---\n{updated_fm}\n---\n{body}"
        vault_fs.write_note(v, note_id, new_content)
        log.info(f"add_tags: {note_id} +{new_tags}")
        return f"Tags adicionadas em '{note_id}': {new_tags}"
    except Exception as e:
        return f"Erro ao adicionar tags em '{note_id}': {e}"


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_vault",
            "description": "Busca semântica nas notas do vault via embeddings. Use para encontrar notas relevantes. Suporta filtro por tags e data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":     {"type": "string", "description": "Texto para buscar semanticamente"},
                    "tags":      {"type": "string", "description": "Filtrar por tag específica (ex: 'haleon', 'mercado')"},
                    "date_from": {"type": "string", "description": "Filtrar notas a partir desta data (YYYY-MM-DD)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_neighbors",
            "description": "Retorna notas diretamente conectadas a uma nota via wiki links. Use para navegar no grafo de conhecimento.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "ID da nota para verificar conexões"},
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "graph_search",
            "description": "BFS no grafo de links a partir de uma nota. Mapeia a rede de conhecimento conectada. Use para encontrar clusters temáticos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_note_id": {"type": "string", "description": "Nota inicial para o BFS"},
                    "depth":         {"type": "integer", "description": "Profundidade do BFS (1=vizinhos diretos, 2=vizinhos de vizinhos). Padrão: 2"},
                },
                "required": ["start_note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": "Lê o conteúdo completo de uma nota pelo seu ID (caminho).",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "ID da nota, ex: '06-reunioes/reuniao-x.md'"},
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "Lista todas as notas ativas do vault.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_note",
            "description": "Edita o conteúdo de uma nota existente. Sempre leia a nota antes de editar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "ID da nota a editar"},
                    "content": {"type": "string", "description": "Novo conteúdo completo da nota em markdown"},
                },
                "required": ["note_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": "Cria uma nova nota no vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "Caminho da nota, ex: '00-inbox/nova-nota.md'"},
                    "content": {"type": "string", "description": "Conteúdo da nota em markdown"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_note",
            "description": "Move ou renomeia uma nota. Copia o conteúdo para o novo caminho e marca o original como deletado. Use para reorganizar pastas ou corrigir nomes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string", "description": "Caminho atual da nota"},
                    "dest_id":   {"type": "string", "description": "Novo caminho da nota"},
                },
                "required": ["source_id", "dest_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_note",
            "description": "Deleta permanentemente uma nota. Use SOMENTE para duplicatas confirmadas ou notas que o usuário pediu para remover.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "Caminho da nota a deletar"},
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_tags",
            "description": "Adiciona tags ao frontmatter YAML de uma nota sem sobrescrever o conteúdo. Mais seguro que edit_note para mudanças só de tags.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "Caminho da nota"},
                    "tags":    {"type": "array", "items": {"type": "string"}, "description": "Tags a adicionar, ex: ['politica', 'analise', 'brasil']"},
                },
                "required": ["note_id", "tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ensure_settings",
            "description": "Cria os arquivos de configuração do agente (profile.md e load.md) em 00-meta/config/ se não existirem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vault": {"type": "string", "description": "Nome do vault (subdiretório em disco)"},
                },
                "required": ["vault"],
            },
        },
    },
]

TOOL_FNS = {
    "search_vault":    lambda args: tool_search_vault(**args),
    "get_neighbors":   lambda args: tool_get_neighbors(**args),
    "graph_search":    lambda args: tool_graph_search(**args),
    "read_note":       lambda args: tool_read_note(**args),
    "list_notes":      lambda args: tool_list_notes(),
    "edit_note":       lambda args: tool_edit_note(**args),
    "create_note":     lambda args: tool_create_note(**args),
    "move_note":       lambda args: tool_move_note(**args),
    "delete_note":     lambda args: tool_delete_note(**args),
    "add_tags":        lambda args: tool_add_tags(**args),
    "ensure_settings": lambda args: tool_ensure_settings(**args),
}

SYSTEM_PROMPT = """Você é o Mordomo do Conhecimento — um agente autônomo que organiza, conecta e enriquece o vault Obsidian do usuário.

MISSÃO:
Transformar informações brutas em conhecimento navegável. Você decide a estrutura de pastas, cria notas, estabelece conexões e mantém o vault sempre organizado e coerente. Você age como um mordomo inteligente: antecipa necessidades, organiza sem pedir permissão, e executa tarefas até o fim — sem parar no meio, sem pedir confirmação.

ESTRUTURA DE PASTAS — LIVRE:
- Você pode criar qualquer pasta e subpasta que faça sentido para o contexto
- Prefira hierarquias semânticas: "projetos/nome-do-projeto/" ao invés de "01-projetos/"
- Use slugs em lowercase com hifens, sem acentos: "reuniao-kick-off.md"

FORMATO OBRIGATÓRIO — SEM EXCEÇÃO:
- Responda SEMPRE com exatamente um objeto JSON por mensagem
- NUNCA misture texto com JSON
- NUNCA escreva explicações fora do JSON
- Use SOMENTE estes formatos:

{"tool": "search_vault", "args": {"query": "...", "tags": "opcional", "date_from": "opcional YYYY-MM-DD"}}
{"tool": "get_neighbors", "args": {"note_id": "caminho/nota.md"}}
{"tool": "graph_search", "args": {"start_note_id": "caminho/nota.md", "depth": 2}}
{"tool": "read_note", "args": {"note_id": "caminho/nota.md"}}
{"tool": "list_notes", "args": {}}
{"tool": "edit_note", "args": {"note_id": "caminho/nota.md", "content": "conteúdo markdown completo"}}
{"tool": "create_note", "args": {"path": "pasta/subpasta/nota.md", "content": "conteúdo markdown completo"}}
{"tool": "move_note", "args": {"source_id": "caminho/atual.md", "dest_id": "caminho/novo.md"}}
{"tool": "delete_note", "args": {"note_id": "caminho/nota.md"}}
{"tool": "add_tags", "args": {"note_id": "caminho/nota.md", "tags": ["tag1", "tag2"]}}
{"tool": "ensure_settings", "args": {"vault": "nome-do-vault"}}
{"tool": "done", "args": {"answer": "resumo completo do que foi feito"}}

USO EFICIENTE DAS FERRAMENTAS — OBRIGATÓRIO:

❌ NUNCA faça list_notes + read_note em loop para analisar o vault. Isso é O(n) e lento.
✅ USE search_vault com queries temáticas para descobrir clusters de conteúdo.

QUANDO ANALISAR ESTRUTURA, CORRELAÇÕES OU ORGANIZAÇÃO:
1. Faça várias chamadas search_vault com queries temáticas (ex: "reunião projeto", "análise mercado", "empresa parceiro", "decisão estratégica", etc.)
2. Use graph_search nas notas mais conectadas para mapear clusters
3. Use list_notes APENAS para obter a lista de caminhos/pastas existentes — nunca para ler conteúdo
4. Use read_note APENAS quando precisar do conteúdo COMPLETO de uma nota específica já identificada

QUANDO USAR CADA FERRAMENTA:
- search_vault → descoberta semântica, correlações, "quais notas falam sobre X?"
- graph_search → mapear redes de conhecimento, clusters conectados por wiki links
- get_neighbors → explorar conexões de uma nota específica já conhecida
- list_notes → ver estrutura de pastas e caminhos (não conteúdo)
- read_note → ler conteúdo de nota JÁ identificada como relevante

GRAFO DE CONHECIMENTO:
- get_neighbors: vizinhos diretos (1 hop) de uma nota — rápido para explorar conexões imediatas
- graph_search: BFS depth=2 para mapear clusters temáticos — use quando precisar entender contexto amplo
- search_vault retorna connected_notes de cada resultado (notas conectadas no grafo)

REGRAS DE EXECUÇÃO:
- Execute a tarefa COMPLETA até o fim. Se há 50 notas para processar, processe as 50.
- Não pare no meio para "listar pendências" — execute as pendências.
- Se uma subtarefa falhar, continue com as próximas sem parar.
- Ao reorganizar pastas: use move_note (não criar + deletar manualmente).
- Ao padronizar tags: use add_tags (mais seguro que edit_note para só adicionar tags).
- Ao deletar duplicatas: confirme que o conteúdo foi movido antes de usar delete_note.

RESPEITO À INTENÇÃO DO USUÁRIO — CRÍTICO:
- Se o usuário disser "só analise", "não mova ainda", "mostre primeiro", "me pergunte antes" → chame done com a PROPOSTA em texto. NUNCA execute move_note/edit_note/delete_note/create_note sem confirmação explícita.
- Se o usuário disser "faça", "execute", "aplique", "pode mover" → execute sem pedir confirmação.
- Dúvida sobre a intenção? Trate como "só análise" e apresente a proposta no done.

RASTREAMENTO DE ESTADO — CRÍTICO:
- Antes de mover uma nota, verifique mentalmente se ela já foi movida nesta sessão.
- NUNCA tente mover uma nota para o mesmo caminho onde ela já está.
- Se um move_note retornar erro indicando que a nota não existe no source, ela já foi movida — pule.
- Mantenha uma lista mental de "já processados" e não processe duas vezes.

REGRAS DE KNOWLEDGE GRAPH:
- Wiki links SEMPRE com alias: [[caminho/nota|Nome Visível]]
- Preserve frontmatter YAML ao editar notas
- Frontmatter mínimo: title, date, tags

CONCLUSÃO:
- Só chame "done" quando TUDO estiver feito
- No "done", liste cada nota movida/editada/criada/deletada com uma linha de descrição"""


# ── System prompt para o Claude Agent SDK (Fase 2) ──────────────────────────────
# O SDK chama ferramentas nativamente (sem formato JSON). Mantém persona e regras.
SDK_SYSTEM_PROMPT = """Você é o Mordomo do Conhecimento — um agente que organiza, conecta e enriquece o vault Obsidian do usuário.

MISSÃO:
Transformar informações brutas em conhecimento navegável. Você decide a estrutura de pastas, cria notas, estabelece conexões e mantém o vault organizado e coerente. Antecipa necessidades e executa a tarefa até o fim, sem parar no meio.

FERRAMENTAS (use as ferramentas disponíveis — não descreva chamadas em texto):
- search_vault → descoberta semântica: "quais notas falam sobre X?" (retorna notas conectadas no grafo)
- graph_search / get_neighbors → mapear clusters e conexões via wiki links
- list_notes → ver caminhos/estrutura de pastas (NUNCA para ler conteúdo em loop)
- read_note → ler o conteúdo de uma nota já identificada como relevante
- create_note / edit_note / move_note / delete_note / add_tags → alterações no vault
- query_events → linha do tempo de atividade (o que você e o usuário fizeram, com data/hora)

PERGUNTAS SOBRE UM PERÍODO (ex.: "resumo entre X e Y", "o que fiz essa semana"):
- Comece por query_events(date_from, date_to) para obter a linha do tempo.
- Depois aprofunde no vault (read_note/search_vault) SÓ nos itens que precisam de mais contexto.
- Monte a narrativa combinando o "quando/o quê" (eventos) com o "sobre o quê" (notas).

USO EFICIENTE — OBRIGATÓRIO:
- NUNCA faça list_notes + read_note em loop para analisar o vault (é O(n) e lento).
- Use várias chamadas search_vault com queries temáticas para descobrir clusters.
- Use read_note só para notas específicas já identificadas como relevantes.

ESTRUTURA DE PASTAS — LIVRE:
- Crie pastas/subpastas que façam sentido; prefira hierarquias semânticas ("projetos/nome/").
- Slugs em lowercase com hifens, sem acentos.

RESPEITO À INTENÇÃO DO USUÁRIO — CRÍTICO:
- Se o usuário disser "só analise", "não mova ainda", "mostre primeiro", "me pergunte antes" → responda com a PROPOSTA em texto e NÃO use create_note/edit_note/move_note/delete_note.
- Se disser "faça", "execute", "aplique" → execute sem pedir confirmação.
- Em dúvida, trate como "só análise" e apresente a proposta.

EXECUÇÃO:
- Execute a tarefa COMPLETA. Se há 50 notas para processar, processe as 50.
- Ao reorganizar: use move_note (não criar+deletar). Ao padronizar tags: use add_tags.
- Nunca mova uma nota para o caminho onde ela já está; se o source não existe, já foi movida — pule.

KNOWLEDGE GRAPH:
- Wiki links SEMPRE com alias: [[caminho/nota|Nome Visível]]. Preserve o frontmatter YAML ao editar.
- Frontmatter mínimo: title, date, tags.

RESPOSTA FINAL:
- Ao terminar, escreva um resumo em texto listando cada nota criada/editada/movida/deletada com uma linha de descrição. Não peça para continuar — conclua."""


def _call_llm_raw(messages: list) -> str:
    """Chamada única ao LLM (Claude Agent SDK, sem ferramentas). Converte o formato
    de mensagens (system + user/assistant) em system+user para o complete do SDK."""
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    rest   = [m for m in messages if m.get("role") != "system"]
    user   = "\n\n".join(
        (m["content"] if m.get("role") == "user" else f"[assistente anterior]\n{m['content']}")
        for m in rest
    )
    return sdk_llm.complete(system, user).strip()


GENERATE_TEMPLATES = {
    "relatorio": {
        "label": "Relatório",
        "system": "Você é um analista especialista. Gere um relatório estruturado, profissional e baseado APENAS nos dados do vault fornecidos.",
        "format": "# {titulo}\n\n## Resumo Executivo\n\n## Contexto\n\n## Análise\n\n## Conclusões\n\n## Recomendações",
    },
    "email": {
        "label": "Email",
        "system": "Você é um assistente de comunicação. Gere um email profissional e objetivo baseado APENAS nos dados do vault fornecidos.",
        "format": "**Assunto:** ...\n\nOlá [nome],\n\n[corpo do email]\n\nAtenciosamente,\n[remetente]",
    },
    "apresentacao": {
        "label": "Apresentação",
        "system": "Você é um consultor de comunicação. Gere o roteiro detalhado de uma apresentação com slides, baseado APENAS nos dados do vault fornecidos.",
        "format": "## Slide 1 — Título e subtítulo\n\n## Slide 2 — Agenda\n\n## Slide N — [tema]\n**Pontos-chave:**\n- ...\n\n## Slide Final — Conclusão e próximos passos",
    },
    "resumo": {
        "label": "Resumo Executivo",
        "system": "Você é um analista sênior. Gere um resumo executivo conciso e direto baseado APENAS nos dados do vault fornecidos.",
        "format": "**Situação:** ...\n\n**Análise:** ...\n\n**Decisão/Recomendação:** ...\n\n**Próximos passos:** ...",
    },
}


def generate_output(topic: str, template: str = "relatorio",
                    vault: Optional[str] = None, extra_note_ids: Optional[list] = None) -> dict:
    """Gera output estruturado (relatório, email, etc.) usando buscas semânticas no vault."""
    tmpl = GENERATE_TEMPLATES.get(template, GENERATE_TEMPLATES["relatorio"])
    db   = vault

    # Múltiplas queries para cobrir diferentes ângulos
    queries = [topic, f"análise {topic}", f"contexto {topic}", f"dados {topic}", f"decisões {topic}"]
    context_chunks: list[str] = []
    seen: set[str] = set()

    for q in queries:
        try:
            raw     = tool_search_vault(q, db=db)
            results = json.loads(raw)
            for r in results[:4]:
                nid = r.get("note_id", "")
                if nid and nid not in seen:
                    seen.add(nid)
                    title   = r.get("title") or nid.split("/")[-1].replace(".md", "")
                    excerpt = r.get("excerpt", "")
                    context_chunks.append(f"### [{nid}] {title}\n{excerpt}")
        except Exception:
            pass

    # Notas extras explicitamente solicitadas
    for nid in (extra_note_ids or [])[:5]:
        if nid not in seen:
            seen.add(nid)
            content = tool_read_note(nid, db=db)
            context_chunks.append(f"### [{nid}]\n{content[:3000]}")

    if not context_chunks:
        return {"answer": f"Nenhuma nota encontrada sobre '{topic}' no vault.", "sources": []}

    context = "\n\n---\n\n".join(context_chunks[:20])
    prompt  = (
        f"Gere um(a) **{tmpl['label']}** sobre o tema: **{topic}**\n\n"
        f"Use SOMENTE as informações das notas do vault abaixo. Não invente dados.\n\n"
        f"Formato esperado:\n{tmpl['format']}\n\n"
        f"NOTAS DO VAULT ({len(context_chunks)} notas):\n\n{context}\n\n"
        f"Gere o(a) {tmpl['label']} completo agora em português:"
    )

    result = _call_llm_raw([
        {"role": "system", "content": tmpl["system"]},
        {"role": "user",   "content": prompt},
    ])

    return {"answer": result, "sources": sorted(seen), "template": template}


def ask(question: str, collections: Optional[list] = None, vault: Optional[str] = None,
        on_progress=None) -> dict:
    """Loop de tool calling via prompt até o agente chamar 'done'.
    vault = nome do subdiretório em disco (se None, usa o vault padrão)."""

    db = _vault(vault)

    # Carrega settings do vault e injeta no system prompt
    settings_ctx = ""
    settings = load_settings(db, db=db)
    parts = []
    if settings.get("profile"):
        parts.append(f"## PERFIL DO AGENTE (do vault)\n{settings['profile']}")
    if settings.get("mem"):
        parts.append(f"## MEMÓRIA DE SESSÕES ANTERIORES\n{settings['mem'][-3000:]}")
    if parts:
        settings_ctx = "\n\n" + "\n\n".join(parts)

    vault_ctx = (
        f"\n\nVAULT ATIVO: '{db}'\n"
        f"- Você trabalha no vault '{db}' (subdiretório em disco).\n"
        f"- As notas de configuração estão em '00-meta/config/'.\n"
        f"- list_notes e search_vault já operam sobre este vault.\n"
    )

    system_prompt = SDK_SYSTEM_PROMPT + settings_ctx + vault_ctx

    reset_stop()
    state.record_event(db, actor="user", kind="pergunta", summary=question[:300], source="chat")
    result = sdk_llm.run_agent(system_prompt, question, db, on_progress=on_progress)
    answer  = result.get("answer", "Concluído.")
    sources = result.get("sources", [])

    # Salva memória da sessão
    try:
        actions_summary = _build_actions_summary(sources, [])
        save_session_memory(db, question, answer, sources, actions_summary, db=db)
    except Exception as e:
        log.warning(f"Falha ao salvar memória de sessão: {e}")

    return {"answer": answer, "sources": sources}


def _build_actions_summary(sources: list, messages: list) -> str:
    """Gera um resumo compacto das ações executadas a partir das fontes e mensagens."""
    if not sources:
        return "nenhuma ação executada"

    # Conta tipos de ação a partir das mensagens de resultado
    counts = {"movidas": 0, "criadas": 0, "editadas": 0, "deletadas": 0, "tags": 0}
    for msg in messages:
        if msg.get("role") == "user":
            c = msg.get("content", "")
            if "Resultado de move_note" in c:
                counts["movidas"] += 1
            elif "Resultado de create_note" in c or "Resultado de ensure_settings" in c:
                counts["criadas"] += 1
            elif "Resultado de edit_note" in c:
                counts["editadas"] += 1
            elif "Resultado de delete_note" in c:
                counts["deletadas"] += 1
            elif "Resultado de add_tags" in c:
                counts["tags"] += 1

    parts = []
    if counts["movidas"]:
        parts.append(f"{counts['movidas']} nota(s) movida(s)")
    if counts["criadas"]:
        parts.append(f"{counts['criadas']} nota(s) criada(s)")
    if counts["editadas"]:
        parts.append(f"{counts['editadas']} nota(s) editada(s)")
    if counts["deletadas"]:
        parts.append(f"{counts['deletadas']} nota(s) deletada(s)")
    if counts["tags"]:
        parts.append(f"{counts['tags']} tag(s) adicionada(s)")

    return ", ".join(parts) if parts else f"{len(set(sources))} nota(s) afetada(s)"


# ── Normalização de tags (operação mecânica, sem LLM) ────────────────────────
def _normalize_tag(tag: str) -> str:
    """Lowercase, sem acentos, espaços e underscores → hífens, remove chars especiais."""
    tag = unicodedata.normalize("NFD", tag)
    tag = "".join(c for c in tag if unicodedata.category(c) != "Mn")
    tag = tag.lower().strip()
    tag = re.sub(r"[\s_]+", "-", tag)
    tag = re.sub(r"[^a-z0-9\-]", "", tag)
    tag = re.sub(r"-+", "-", tag).strip("-")
    return tag


def _fix_codeblock_frontmatter(content: str) -> str:
    """Converte ```yaml\\n...\\n``` no início da nota para frontmatter --- correto."""
    m = re.match(r"^```(?:yaml)?\n(.*?)\n```\s*\n?", content, re.DOTALL)
    if m:
        yaml_body = m.group(1)
        rest = content[m.end():]
        return f"---\n{yaml_body}\n---\n{rest.lstrip()}"
    return content


def _fix_tags_in_frontmatter(fm_body: str) -> str:
    """Normaliza o bloco de tags dentro do frontmatter (lista YAML ou array inline)."""
    def _replace(m):
        block = m.group(0)

        # Array inline: tags: ["crítica pessoal", "Saúde"] ou tags: [tag1, tag2]
        inline = re.match(r'^tags:\s*\[(.+)\]\s*$', block.strip(), re.DOTALL)
        if inline:
            raw_tags = re.findall(r'"([^"]+)"|\'([^\']+)\'|([^,\[\]\s]+)', inline.group(1))
            tags = [_normalize_tag(next(p for p in t if p)) for t in raw_tags]
            tags = [t for t in tags if t]
            return "tags:\n" + "\n".join(f"  - {t}" for t in tags)

        # Lista YAML: tags:\n  - valor
        lines = block.split("\n")
        out = []
        for line in lines:
            item = re.match(r"^(\s*[-*]\s*)(.+)$", line)
            if item:
                raw = item.group(2).strip().strip("\"'")
                norm = _normalize_tag(raw)
                if norm:
                    out.append(f"{item.group(1)}{norm}")
            else:
                out.append(line)
        return "\n".join(out)

    return re.sub(r"^tags:.*?(?=\n\w|\Z)", _replace, fm_body, flags=re.MULTILINE | re.DOTALL)


def normalize_tags(vault: Optional[str] = None) -> dict:
    """Lê todas as notas do vault, normaliza tags no frontmatter (sem LLM).
    Se vault=None, processa todos os vaults disponíveis."""
    vaults = [vault] if vault else get_vaults()
    if not vaults:
        return {"answer": "Nenhum vault encontrado em disco.", "sources": []}

    all_updated, all_errors = [], []

    for _db in vaults:
        for nid in vault_fs.list_notes(_db):
            try:
                content = vault_fs.read_note(_db, nid)
            except Exception as e:
                all_errors.append(f"[{_db}] {nid}: {e}")
                continue

            if not content:
                continue

            # 1. Converte ```yaml ... ``` para frontmatter --- correto
            new_content = _fix_codeblock_frontmatter(content)

            # 2. Normaliza tags dentro do frontmatter ---
            if new_content.startswith("---"):
                end = new_content.find("\n---", 3)
                if end != -1 and "tags:" in new_content[:end]:
                    fm_body    = new_content[3:end]
                    fixed_fm   = _fix_tags_in_frontmatter(fm_body)
                    new_content = f"---{fixed_fm}\n---{new_content[end + 4:]}"
            elif "tags:" not in new_content:
                continue  # sem tags, pula

            if new_content == content:
                continue

            try:
                vault_fs.write_note(_db, nid, new_content)
                all_updated.append(f"{_db}/{nid}")
                log.info(f"normalize_tags: [{_db}] {nid}")
            except Exception as e:
                all_errors.append(f"[{_db}] {nid}: {e}")

    lines = [f"**{len(all_updated)} nota(s) atualizadas**, {len(all_errors)} erro(s)."]
    if all_updated:
        lines.append("\n**Atualizadas:**")
        lines.extend(f"- `{n}`" for n in all_updated)
    if all_errors:
        lines.append("\n**Erros:**")
        lines.extend(f"- {e}" for e in all_errors)

    return {"answer": "\n".join(lines), "sources": all_updated}


# ── Ações rápidas ────────────────────────────────────────────────────────────
def weekly_summary(vault: Optional[str] = None) -> dict:
    return ask("Gere um resumo executivo semanal com: projetos em andamento e status, riscos e bloqueios, próximos passos prioritários, stakeholders que precisam de atenção.", vault=vault)


def market_insights(vault: Optional[str] = None) -> dict:
    return ask("Com base nas análises e estudos registrados, identifique: principais tendências de mercado, oportunidades, riscos competitivos e gaps de conhecimento.", vault=vault)


def summarize_meeting(note_id: str, vault: Optional[str] = None) -> dict:
    return ask(f"Leia a nota {note_id} e gere: resumo executivo, decisões tomadas, action items com responsável e prazo, pontos que precisam de follow-up.", vault=vault)


RELATED_HEADER = "## Notas Relacionadas"
from core.text import RELATED_SECTION_RE as _RELATED_RE


def _set_related_section(content: str, links: list) -> str:
    """Reescreve a seção (auto-gerada) '## Notas Relacionadas' com os links atuais.
    Sem links, remove a seção. Links para notas movidas/apagadas somem sozinhos."""
    body = _RELATED_RE.sub("", content).rstrip()
    if not links:
        return body + "\n"
    return body + "\n\n" + RELATED_HEADER + "\n" + "\n".join(links) + "\n"


def vault_review(vault: Optional[str] = None, only_ids: Optional[set] = None) -> dict:
    """Correlaciona as notas (zero LLM) e mantém a seção '## Notas Relacionadas'.

    only_ids: se informado, só reescreve a seção dessas notas (as novas desde a última
    manutenção) + das notas antigas cuja seção aponta para nota que não existe mais.
    A comparação usa o vault todo (as novas ligam às antigas); só a escrita é restrita.

    - Vetor de cada nota = média dos vetores dos seus chunks (não só o 1º chunk,
      que é dominado pelo frontmatter).
    - Similaridade = cosseno calculado aqui (a coleção do Chroma usa L2; o código
      antigo tratava a distância L2 como cosseno e quase nada passava do limiar).
    - Limiar adaptativo: média + K desvios da similaridade entre todos os pares
      (modelos como o e5 têm similaridade-base alta). CORRELATE_STD_K (padrão 1.5)
      ou CORRELATE_MIN_SIM (limiar fixo) ajustam.
    - Lista de notas e caminhos dos links vêm do DISCO (caminho real, com maiúsculas)."""
    import numpy as np
    MAX_LINKS = int(os.environ.get("CORRELATE_MAX_LINKS", "5"))
    STD_K = float(os.environ.get("CORRELATE_STD_K", "1.5"))
    FIXED = os.environ.get("CORRELATE_MIN_SIM", "").strip()

    vault_name = _vault(vault)
    notes = _disk_notes(vault_name)
    if not notes:
        return {"answer": "Vault vazio.", "sources": []}
    by_id = {n["id"]: n for n in notes}

    existing = {n["link"].lower() for n in notes}
    dead = {n["id"] for n in notes
            if any(r.lower().removesuffix(".md") not in existing for r in n["related"])}
    targets = None if only_ids is None else (set(only_ids) | dead)
    if targets is not None and not targets:
        return {"answer": "🔗 Notas Relacionadas: nenhuma nota nova desde a última manutenção.",
                "sources": [], "edited": [], "not_indexed": 0}

    try:
        col = get_chroma().get_collection(_col_name(vault_name))
    except Exception:
        return {"answer": "ChromaDB: vault ainda não indexado pelo watcher.", "sources": []}

    # Soma dos vetores dos chunks por nota (lido em páginas)
    sums, counts, dims = {}, {}, {}
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
            if nid not in by_id:
                continue
            v = np.asarray(emb, dtype=np.float32)
            dims[v.shape[0]] = dims.get(v.shape[0], 0) + 1
            if nid in sums and sums[nid].shape != v.shape:
                continue
            sums[nid] = sums[nid] + v if nid in sums else v.copy()
            counts[nid] = counts.get(nid, 0) + 1
        offset += len(metas)
        if len(metas) < page:
            break

    if not sums:
        return {"answer": "Nenhuma nota indexada no ChromaDB ainda.", "sources": []}
    dim = max(dims, key=dims.get)          # ignora vetores de um modelo anterior
    ids = [i for i in sums if sums[i].shape[0] == dim]
    if len(ids) < 2:
        return {"answer": "Notas indexadas insuficientes para correlacionar.", "sources": []}
    M = np.stack([sums[i] / counts[i] for i in ids])
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    S = M @ M.T
    n = len(ids)
    tri = S[np.triu_indices(n, 1)]
    thr = float(FIXED) if FIXED else float(tri.mean() + STD_K * tri.std())
    np.fill_diagonal(S, -1.0)

    edited = []
    for idx, nid in enumerate(ids):
        if targets is not None and nid not in targets:
            continue
        row = S[idx]
        order = np.argsort(-row)[:MAX_LINKS]
        neigh = [ids[j] for j in order if row[j] >= thr]
        links = [f"- [[{by_id[t]['link']}|{by_id[t]['title']}]]" for t in neigh]
        path = by_id[nid]["link"] + ".md"
        try:
            content = vault_fs.read_note(vault_name, path)
        except Exception:
            continue
        new_content = _set_related_section(content, links)
        if new_content.rstrip() == content.rstrip():
            continue
        vault_fs.write_note(vault_name, path, new_content)
        edited.append(nid)

    not_indexed = len(notes) - len(ids)
    log.info(f"vault_review: {n} notas, limiar {thr:.3f}, {len(edited)} atualizada(s)")
    if edited:
        state.record_event(vault_name, actor="system", kind="correlate",
                           summary=f"Atualizou 'Notas Relacionadas' em {len(edited)} nota(s)",
                           source="chromadb")
    scope = (f"{len(targets)} nota(s) nova(s)/alterada(s) ou com link quebrado"
             if targets is not None else f"todas as {n} nota(s)")
    lines = [f"🔗 Notas Relacionadas: {scope} · comparadas com {n} nota(s) · limiar {thr:.3f}",
             f"- {len(edited)} nota(s) atualizada(s)"]
    if not_indexed > 0:
        lines.append(f"- {not_indexed} nota(s) do disco ainda não estão no índice "
                     f"(o watcher indexa em instantes)")
    return {"answer": "\n".join(lines), "sources": edited[:20], "edited": edited,
            "not_indexed": max(not_indexed, 0)}
