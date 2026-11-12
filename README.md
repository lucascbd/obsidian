# Mordomo do Conhecimento

Assistente pessoal sobre um vault Obsidian em disco, com busca semântica local.

**Arquitetura (em disco, LXC dedicado):**
- **Obsidian (ignis)** — container; vault Markdown em `VAULT_ROOT`, servido via web.
- **ChromaDB** — container; índice semântico persistente, publicado só em `127.0.0.1:8001`.
- **Python (agente Flask)** — bare (systemd `obsidian-agent`): raciocínio, ingestão e redação (`:8090`).
- **watcher** — bare (systemd `obsidian-watcher`): observa o vault via inotify e mantém o ChromaDB sincronizado.
- **SQLite** (`DATA_DIR/state.db`) — estado de sincronização, IDs processados e grafo de links.

> O agente raciocina via **Claude Agent SDK** autenticado pela assinatura **Claude Pro**
> (`CLAUDE_CODE_OAUTH_TOKEN`). Ingestão de e-mail/Teams via `scripts/outlook_collector.py` (roda no PC).

## Token da assinatura Pro (uma vez)

Gere o token numa máquina com navegador e o Claude Code instalado:

```bash
claude setup-token   # faz login na conta Pro e imprime o token (validade ~1 ano)
```

Cole em `CLAUDE_CODE_OAUTH_TOKEN` no `/etc/obsidian-agent.env`. **Não** defina
`ANTHROPIC_API_KEY` — ela tem precedência e ignoraria o token do Pro.

## Instalação / deploy

```bash
# 1. agent + watcher (Python 3.11 via uv, usuário obsidian uid 1000, unidades systemd)
sudo bash deploy/install.sh

# 2. infra em container (ignis + chroma). Porta do ignis: IGNIS_HOST_PORT em deploy/.env
docker compose -p obsidian-infra -f deploy/docker-compose.infra.yml up -d

# 3. preencha /etc/obsidian-agent.env (token, AGENT_API_KEY) e
systemctl restart obsidian-agent obsidian-watcher
```

Caminhos padrão: vault `/data/compose/113/vaults`, estado `/data/compose/113/data`,
índice `/data/compose/113/chroma`, código `/opt/obsidian`.

> Python 3.11 é obrigatório: `chromadb==0.5.20` depende do `chroma-hnswlib`, que não
> compila no Python 3.13 do Debian trixie. O `install.sh` cuida disso via `uv`.

**Atualizar:** `sudo bash /opt/obsidian/deploy/install.sh && systemctl restart obsidian-agent obsidian-watcher`
(faz `git checkout -f` da branch — alterações locais não commitadas são descartadas).

**Logs:** `journalctl -u obsidian-agent -f` · `journalctl -u obsidian-watcher -f`

## Uso

- **Obsidian (ignis):** `http://<IP>:8081` — edite notas; o watcher indexa em segundos.
- **Agente:** `http://<IP>:8090` — perguntas, ingestão de arquivos/URLs, geração de conteúdo.

Cada subdiretório de `VAULT_ROOT` é um vault. Notas em `00-meta/` (índices e config do
agente) não entram na busca.
