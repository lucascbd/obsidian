# Mordomo do Conhecimento

Assistente pessoal sobre um vault Obsidian em disco, com busca semântica local.

**Arquitetura (em disco):**
- **Obsidian (ignis)** — vault Markdown em `./vaults`, servido via web (`:8080`).
- **Python (agente Flask)** — raciocínio, ingestão e redação (`:8090`).
- **ChromaDB** — índice semântico persistente (`:8000`).
- **SQLite** (`./data/state.db`) — estado de sincronização, IDs processados e grafo de links.
- **watcher** — observa o vault via inotify (watchdog) e mantém o ChromaDB sincronizado.

> **Python (agente)** raciocina via **Claude Agent SDK** autenticado pela assinatura
> **Claude Pro** (`CLAUDE_CODE_OAUTH_TOKEN`). Fase 3: ingestão de e-mail/Teams via Microsoft Graph.

## Token da assinatura Pro (uma vez)

O agente usa sua assinatura Claude Pro via Claude Agent SDK. Gere o token numa máquina
com navegador e o Claude Code instalado:

```bash
claude setup-token   # faz login na conta Pro e imprime o token (validade ~1 ano)
```

Cole o valor em `CLAUDE_CODE_OAUTH_TOKEN` nas variáveis da stack. **Não** defina
`ANTHROPIC_API_KEY` — ela tem precedência e ignoraria o token do Pro. O uso conta
contra os limites da assinatura.

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
| `CLAUDE_CODE_OAUTH_TOKEN` | token da assinatura Pro (`claude setup-token`) |
| `ANTHROPIC_MODEL` | `sonnet` (ou `opus`/`haiku`) |
| `IGNIS_HOST_PORT` / `AGENT_HOST_PORT` | portas no host (padrão 8080 / 8090) |
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
