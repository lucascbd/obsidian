#!/usr/bin/env bash
# install.sh — instala/atualiza o agent + watcher BARE num LXC (systemd).
# ignis + chromadb continuam em container (deploy/docker-compose.infra.yml).
# Idempotente: rode de novo a cada deploy (git pull embutido).
#
#   sudo AGENT_HOME=/opt/obsidian bash deploy/install.sh
#
set -euo pipefail

AGENT_HOME="${AGENT_HOME:-/opt/obsidian}"
REPO="${REPO:-https://github.com/lucascbd/obsidian.git}"
BRANCH="${BRANCH:-feature/disk-architecture}"
VAULT_ROOT="${VAULT_ROOT:-/data/compose/113/vaults}"
ENV_FILE="/etc/obsidian-agent.env"

echo "== Mordomo do Conhecimento — instalação bare =="
echo "AGENT_HOME=$AGENT_HOME  BRANCH=$BRANCH"

if [ "$(id -u)" -ne 0 ]; then echo "Rode como root (sudo)."; exit 1; fi

# 1. Dependências de sistema: Python, git, curl, Node 20 + Claude Code CLI
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends python3 python3-venv python3-pip git curl ca-certificates
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y --no-install-recommends nodejs
fi
npm install -g @anthropic-ai/claude-code

# 2. Código: clona, ou inicializa git no diretório existente (mesmo não-vazio), ou atualiza
mkdir -p "$AGENT_HOME"
if [ ! -d "$AGENT_HOME/.git" ]; then
  git -C "$AGENT_HOME" init -q
  git -C "$AGENT_HOME" remote add origin "$REPO" 2>/dev/null || git -C "$AGENT_HOME" remote set-url origin "$REPO"
fi
git -C "$AGENT_HOME" fetch --depth 1 origin "$BRANCH"
git -C "$AGENT_HOME" checkout -f -B "$BRANCH" "origin/$BRANCH"

# 3. venv + dependências Python (agent + watcher num só venv)
python3 -m venv "$AGENT_HOME/.venv"
"$AGENT_HOME/.venv/bin/pip" install --upgrade pip
"$AGENT_HOME/.venv/bin/pip" install -r "$AGENT_HOME/agent/requirements.txt" -r "$AGENT_HOME/watcher/requirements.txt"

# 4. Pré-baixa o modelo de embeddings (custo zero de tokens)
"$AGENT_HOME/.venv/bin/python" -c "from fastembed import TextEmbedding; TextEmbedding('intfloat/multilingual-e5-large')" || true

# 5. Arquivo de env (segredos) — cria o template na 1ª vez, sem sobrescrever
if [ ! -f "$ENV_FILE" ]; then
  cp "$AGENT_HOME/deploy/obsidian-agent.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo ">>> Preencha os segredos em $ENV_FILE (CLAUDE_CODE_OAUTH_TOKEN, AGENT_API_KEY)."
fi

# 6. Usuário de execução = dono do vault (compatível com o ignis)
RUN_USER="$(stat -c '%U' "$VAULT_ROOT" 2>/dev/null || echo root)"
echo "RUN_USER=$RUN_USER (dono de $VAULT_ROOT)"

# 7. Instala as unidades systemd (resolve placeholders)
for svc in obsidian-agent obsidian-watcher; do
  sed -e "s#__AGENT_HOME__#$AGENT_HOME#g" -e "s#__RUN_USER__#$RUN_USER#g" \
      "$AGENT_HOME/deploy/$svc.service" > "/etc/systemd/system/$svc.service"
done
systemctl daemon-reload
systemctl enable obsidian-agent obsidian-watcher

echo
echo "== Pronto. Próximos passos =="
echo "1) Suba a infra (ignis + chroma):"
echo "   docker compose -f $AGENT_HOME/deploy/docker-compose.infra.yml up -d"
echo "2) Preencha $ENV_FILE e então:"
echo "   systemctl restart obsidian-agent obsidian-watcher"
echo "3) Logs:  journalctl -u obsidian-agent -f   |   journalctl -u obsidian-watcher -f"
echo
echo "Deploy futuro:  sudo AGENT_HOME=$AGENT_HOME bash $AGENT_HOME/deploy/install.sh && systemctl restart obsidian-agent obsidian-watcher"
