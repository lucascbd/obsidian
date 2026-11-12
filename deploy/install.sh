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
DATA_DIR="${DATA_DIR:-/data/compose/113/data}"
ENV_FILE="/etc/obsidian-agent.env"
# chromadb==0.5.20 depende do chroma-hnswlib, que não tem wheel p/ Python 3.13
# (Debian trixie) e não compila. Usamos o mesmo Python da imagem Docker via uv.
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
RUN_USER="${RUN_USER:-obsidian}"
RUN_UID="${RUN_UID:-1000}"          # = PUID do ignis (dono do vault)
RUN_HOME="/var/lib/$RUN_USER"
export PATH="/usr/local/bin:$PATH"
export UV_PYTHON_INSTALL_DIR=/opt/uv-python

echo "== Mordomo do Conhecimento — instalação bare =="
echo "AGENT_HOME=$AGENT_HOME  BRANCH=$BRANCH"

if [ "$(id -u)" -ne 0 ]; then echo "Rode como root (sudo)."; exit 1; fi

# 1. Dependências de sistema: git, curl, Node 20 + Claude Code CLI, uv
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends git curl ca-certificates
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y --no-install-recommends nodejs
fi
npm install -g @anthropic-ai/claude-code
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
fi

# 2. Código: clona, ou inicializa git no diretório existente (mesmo não-vazio), ou atualiza
mkdir -p "$AGENT_HOME"
if [ ! -d "$AGENT_HOME/.git" ]; then
  git -C "$AGENT_HOME" init -q
  git -C "$AGENT_HOME" remote add origin "$REPO" 2>/dev/null || git -C "$AGENT_HOME" remote set-url origin "$REPO"
fi
git -C "$AGENT_HOME" fetch --depth 1 origin "$BRANCH"
git -C "$AGENT_HOME" checkout -f -B "$BRANCH" "origin/$BRANCH"

# 3. Usuário de execução (uid = PUID do ignis, para dividir o vault sem conflito)
if ! id "$RUN_USER" >/dev/null 2>&1; then
  getent group "$RUN_UID" >/dev/null || groupadd -g "$RUN_UID" "$RUN_USER"
  useradd -r -u "$RUN_UID" -g "$RUN_UID" -d "$RUN_HOME" -m -s /usr/sbin/nologin "$RUN_USER"
fi
mkdir -p "$RUN_HOME" "$DATA_DIR"
chown "$RUN_USER:" "$RUN_HOME"
chown -R "$RUN_USER:" "$DATA_DIR"

# 4. venv (Python $PYTHON_VERSION via uv) + dependências (agent + watcher num só venv)
uv python install "$PYTHON_VERSION"
if [ ! -x "$AGENT_HOME/.venv/bin/python" ] || \
   ! "$AGENT_HOME/.venv/bin/python" -c "import sys; assert '%d.%d' % sys.version_info[:2] == '$PYTHON_VERSION'" 2>/dev/null; then
  rm -rf "$AGENT_HOME/.venv"
  uv venv --python "$PYTHON_VERSION" "$AGENT_HOME/.venv"
fi
uv pip install --python "$AGENT_HOME/.venv/bin/python" pip \
  -r "$AGENT_HOME/agent/requirements.txt" -r "$AGENT_HOME/watcher/requirements.txt"

# 5. Pré-baixa o modelo de embeddings num cache persistente (/tmp é tmpfs no LXC)
runuser -u "$RUN_USER" -- env HOME="$RUN_HOME" FASTEMBED_CACHE_PATH="$RUN_HOME/fastembed" \
  "$AGENT_HOME/.venv/bin/python" -c "from fastembed import TextEmbedding; TextEmbedding('intfloat/multilingual-e5-large')" || true

# 6. Arquivo de env (segredos) — cria o template na 1ª vez, sem sobrescrever
if [ ! -f "$ENV_FILE" ]; then
  cp "$AGENT_HOME/deploy/obsidian-agent.env.example" "$ENV_FILE"
  sed -i "s|^FLASK_SECRET_KEY=.*|FLASK_SECRET_KEY=$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')|" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo ">>> Preencha os segredos em $ENV_FILE (CLAUDE_CODE_OAUTH_TOKEN, AGENT_API_KEY)."
fi

# 7. Instala as unidades systemd (resolve placeholders)
for svc in obsidian-agent obsidian-watcher; do
  sed -e "s#__AGENT_HOME__#$AGENT_HOME#g" -e "s#__RUN_USER__#$RUN_USER#g" -e "s#__RUN_HOME__#$RUN_HOME#g" \
      "$AGENT_HOME/deploy/$svc.service" > "/etc/systemd/system/$svc.service"
done
systemctl daemon-reload
systemctl enable obsidian-agent obsidian-watcher

echo
echo "== Pronto. Próximos passos =="
echo "1) Suba a infra (ignis + chroma em 127.0.0.1:8001):"
echo "   docker compose -p obsidian-infra -f $AGENT_HOME/deploy/docker-compose.infra.yml up -d"
echo "2) Preencha $ENV_FILE e então:"
echo "   systemctl restart obsidian-agent obsidian-watcher"
echo "3) Logs:  journalctl -u obsidian-agent -f   |   journalctl -u obsidian-watcher -f"
echo
echo "Deploy futuro:  sudo AGENT_HOME=$AGENT_HOME bash $AGENT_HOME/deploy/install.sh && systemctl restart obsidian-agent obsidian-watcher"
