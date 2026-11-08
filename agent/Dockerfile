# Build a partir da raiz do repo (contexto=.) para incluir o pacote core/.
FROM python:3.11-slim

WORKDIR /app

# Node 20 + Claude Code CLI (o claude-agent-sdk roda sobre ele).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pré-baixa o modelo de embeddings (multilingual-e5-large: 1024 dims).
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('intfloat/multilingual-e5-large')"

COPY core/ ./core/
COPY agent/agent.py agent/app.py agent/sdk_llm.py agent/organizer.py ./

EXPOSE 8090

CMD ["python", "-u", "app.py"]
