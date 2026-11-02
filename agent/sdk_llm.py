"""sdk_llm.py — camada LLM via Claude Agent SDK (Fase 2).

Substitui o loop manual OpenRouter. Expõe as operações do vault como um MCP
server in-process (o SDK chama as ferramentas nativamente) e mapeia o stream de
mensagens para o callback de progresso usado pela UI (SSE).

Auth: o SDK usa CLAUDE_CODE_OAUTH_TOKEN (assinatura Claude Pro) automaticamente.
IMPORTANTE: ANTHROPIC_API_KEY tem precedência — se estiver no ambiente, o token
da assinatura é ignorado; por isso removemos a var no import.
"""

import os
import json
import asyncio
import logging

log = logging.getLogger(__name__)

# Garante que o token da assinatura seja usado (API key teria precedência).
os.environ.pop("ANTHROPIC_API_KEY", None)

MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", 40))


def current_model() -> str:
    """Modelo escolhido nas settings (fallback para o env/padrão)."""
    try:
        from core.settings import get_settings
        return get_settings().get("model") or os.environ.get("ANTHROPIC_MODEL", "sonnet")
    except Exception:
        return os.environ.get("ANTHROPIC_MODEL", "sonnet")

_SERVER_NAME = "vault"


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)}]}


def _build_server(db, sources: list, on_progress):
    """Cria o MCP server in-process com as ferramentas do vault ligadas a um vault (db).
    `sources` acumula note_ids modificados; `on_progress` é chamado a cada tool call."""
    from claude_agent_sdk import tool, create_sdk_mcp_server
    import agent  # import tardio evita ciclo no import do módulo

    def _progress(name, detail):
        if on_progress:
            try:
                on_progress({"tool": name, "args": {}, "result_preview": str(detail)[:120]})
            except Exception:
                pass

    async def _run(fn, *a, **k):
        return await asyncio.to_thread(fn, *a, **k)

    @tool("search_vault", "Busca semântica nas notas do vault (enriquecida com o grafo de links). Use para encontrar notas relevantes antes de agir.",
          {"type": "object", "properties": {
              "query": {"type": "string"},
              "tags": {"type": "string"},
              "date_from": {"type": "string"}},
           "required": ["query"]})
    async def search_vault(args):
        _progress("search_vault", args.get("query", ""))
        q = {k: v for k, v in args.items() if k in ("query", "tags", "date_from")}
        return _text(await _run(agent._graph_enrich_search, q, db=db))

    @tool("get_neighbors", "Notas diretamente conectadas a uma nota via wiki links (vizinhos no grafo).",
          {"type": "object", "properties": {"note_id": {"type": "string"}}, "required": ["note_id"]})
    async def get_neighbors(args):
        _progress("get_neighbors", args.get("note_id", ""))
        return _text(await _run(agent.tool_get_neighbors, args["note_id"], db=db))

    @tool("graph_search", "BFS no grafo de links a partir de uma nota (clusters temáticos).",
          {"type": "object", "properties": {"start_note_id": {"type": "string"}, "depth": {"type": "integer"}},
           "required": ["start_note_id"]})
    async def graph_search(args):
        _progress("graph_search", args.get("start_note_id", ""))
        return _text(await _run(agent.tool_graph_search, args["start_note_id"], args.get("depth", 2), db=db))

    @tool("read_note", "Lê o conteúdo completo de uma nota pelo seu note_id (caminho relativo ao vault).",
          {"type": "object", "properties": {"note_id": {"type": "string"}}, "required": ["note_id"]})
    async def read_note(args):
        _progress("read_note", args.get("note_id", ""))
        return _text(await _run(agent.tool_read_note, args["note_id"], db=db))

    @tool("list_notes", "Lista todas as notas do vault (exclui 00-meta).",
          {"type": "object", "properties": {}})
    async def list_notes(args):
        _progress("list_notes", "")
        return _text(await _run(agent.tool_list_notes, db=db))

    @tool("create_note", "Cria (ou sobrescreve) uma nota. path = caminho relativo, ex: 'projetos/x/nota.md'.",
          {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
           "required": ["path", "content"]})
    async def create_note(args):
        _progress("create_note", args.get("path", ""))
        sources.append(args.get("path", ""))
        return _text(await _run(agent.tool_create_note, args["path"], args["content"], db=db))

    @tool("edit_note", "Edita (sobrescreve) o conteúdo de uma nota existente. Leia antes de editar.",
          {"type": "object", "properties": {"note_id": {"type": "string"}, "content": {"type": "string"}},
           "required": ["note_id", "content"]})
    async def edit_note(args):
        _progress("edit_note", args.get("note_id", ""))
        sources.append(args.get("note_id", ""))
        return _text(await _run(agent.tool_edit_note, args["note_id"], args["content"], db=db))

    @tool("move_note", "Move/renomeia uma nota (source_id → dest_id).",
          {"type": "object", "properties": {"source_id": {"type": "string"}, "dest_id": {"type": "string"}},
           "required": ["source_id", "dest_id"]})
    async def move_note(args):
        _progress("move_note", f"{args.get('source_id','')} → {args.get('dest_id','')}")
        sources.append(args.get("dest_id", ""))
        return _text(await _run(agent.tool_move_note, args["source_id"], args["dest_id"], db=db))

    @tool("delete_note", "Deleta uma nota. Use só para duplicatas confirmadas ou a pedido do usuário.",
          {"type": "object", "properties": {"note_id": {"type": "string"}}, "required": ["note_id"]})
    async def delete_note(args):
        _progress("delete_note", args.get("note_id", ""))
        sources.append(args.get("note_id", ""))
        return _text(await _run(agent.tool_delete_note, args["note_id"], db=db))

    @tool("add_tags", "Adiciona tags ao frontmatter de uma nota (sem duplicar).",
          {"type": "object", "properties": {"note_id": {"type": "string"},
                                             "tags": {"type": "array", "items": {"type": "string"}}},
           "required": ["note_id", "tags"]})
    async def add_tags(args):
        _progress("add_tags", f"{args.get('note_id','')} +{args.get('tags',[])}")
        sources.append(args.get("note_id", ""))
        return _text(await _run(agent.tool_add_tags, args["note_id"], args["tags"], db=db))

    tools = [search_vault, get_neighbors, graph_search, read_note, list_notes,
             create_note, edit_note, move_note, delete_note, add_tags]
    return create_sdk_mcp_server(name=_SERVER_NAME, version="1.0.0", tools=tools)


def _options(system_prompt: str, db, sources: list, on_progress):
    from claude_agent_sdk import ClaudeAgentOptions
    server = _build_server(db, sources, on_progress)
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=current_model(),
        mcp_servers={_SERVER_NAME: server},
        allowed_tools=[f"mcp__{_SERVER_NAME}__*"],
        tools=[],                     # desliga as ferramentas nativas (só MCP do vault)
        # "default" + allowed_tools pré-aprova as tools do vault sem prompt.
        # (bypassPermissions vira --dangerously-skip-permissions, que o CLI recusa como root.)
        permission_mode="default",
        setting_sources=[],           # não lê settings/CLAUDE.md do filesystem
        max_turns=MAX_TURNS,
    )


async def _run_agent_async(system_prompt: str, question: str, db, sources: list, on_progress):
    from claude_agent_sdk import ClaudeSDKClient, AssistantMessage, TextBlock, ToolUseBlock, ResultMessage
    opts = _options(system_prompt, db, sources, on_progress)
    answer, cost = "", None
    async with ClaudeSDKClient(options=opts) as client:
        await client.query(question)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        answer = block.text
            elif isinstance(msg, ResultMessage):
                if getattr(msg, "result", None):
                    answer = msg.result
                cost = getattr(msg, "total_cost_usd", None)
    if cost is not None:
        log.info(f"[sdk] custo estimado: ${cost}")
    return answer


def run_agent(system_prompt: str, question: str, db, on_progress=None) -> dict:
    """Executa o agente (sync wrapper). Retorna {'answer', 'sources'}."""
    sources: list = []
    try:
        answer = asyncio.run(_run_agent_async(system_prompt, question, db, sources, on_progress))
    except Exception as e:
        log.error(f"[sdk] erro no agente: {e}")
        return {"answer": f"Erro ao executar o agente (Claude Agent SDK): {e}", "sources": []}
    return {"answer": answer or "Concluído.", "sources": sorted({s for s in sources if s})}


async def _complete_async(system: str, user: str) -> str:
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage, TextBlock
    opts = ClaudeAgentOptions(system_prompt=system, tools=[], max_turns=1,
                              model=current_model(), setting_sources=[], permission_mode="default")
    out = ""
    async for msg in query(prompt=user, options=opts):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text:
                    out = block.text
        elif isinstance(msg, ResultMessage) and getattr(msg, "result", None):
            out = msg.result
    return out


def complete(system: str, user: str) -> str:
    """Chamada única (sem ferramentas) — redação/geração. Retorna texto."""
    try:
        return asyncio.run(_complete_async(system, user))
    except Exception as e:
        log.error(f"[sdk] erro no complete: {e}")
        return f"Erro ao chamar o LLM (Claude Agent SDK): {e}"
