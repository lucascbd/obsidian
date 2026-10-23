# Mordomo do Conhecimento

Assistente pessoal sobre um vault Obsidian em disco, com busca semântica local.

**Arquitetura (em disco):**
- **Obsidian (ignis)** — vault Markdown em `./vaults`, servido via web (`:8080`).
- **Python (agente Flask)** — raciocínio, ingestão e redação (`:8090`).
- **ChromaDB** — índice semântico persistente (`:8000`).
- **SQLite** (`./data/state.db`) — estado de sincronização, IDs processados e grafo de links.
- **watcher** — observa o vault via inotify (watchdog) e mantém o ChromaDB sincronizado.

> Fase 1 usa o OpenRouter como LLM. Fase 2 migra o raciocínio para o **Claude Agent SDK**
> (`ANTHROPIC_API_KEY`). Fase 3: ingestão de e-mail/Teams via Microsoft Graph.

## Deploy via Portainer (Git)

### 1. Stacks → Add stack → Repository

| Campo | Valor |
|-------|-------|
| Repository URL | `https://github.com/lucascbd/obsidian` |
| Branch | `main` |
| Compose path | `docker-compose.yml` |

### 2. Variáveis de ambiente

| Variável | Valor |
|----------|-------|
| `OBSIDIAN_VERSION` | `1.12.7` |
| `PUID` / `PGID` | `1000` / `1000` |
| `DEFAULT_VAULT` | nome do subdiretório em `./vaults` (vazio = primeiro encontrado) |
| `OPENROUTER_API_KEY` | sua chave do OpenRouter (Fase 1) |
| `OPENROUTER_MODEL` | `nousresearch/hermes-3-llama-3.1-70b` |
| `ANTHROPIC_API_KEY` | chave Anthropic (Fase 2) |
| `FLASK_SECRET_KEY` | string aleatória |

### 3. Deploy the stack

**Deploy the stack** — o Portainer builda e sobe tudo. Os volumes `./vaults` e `./data`
persistem no host.

---

## Uso

- **Obsidian (ignis):** `http://<IP>:8080` — edite notas; o watcher indexa em segundos.
- **Agente:** `http://<IP>:8090` — perguntas, ingestão de arquivos/URLs, geração de conteúdo.

Cada subdiretório de `./vaults` é um vault. Notas em `00-meta/` (índices e config do
agente) não entram na busca.

## Atualizar após mudanças no código

Portainer → Stacks → **Pull and redeploy**.
