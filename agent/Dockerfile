# Build a partir da raiz do repo (contexto=.) para incluir o pacote core/.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pré-baixa o modelo de embeddings (multilingual-e5-large: 1024 dims).
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('intfloat/multilingual-e5-large')"

COPY core/ ./core/
COPY agent/agent.py agent/app.py ./

EXPOSE 8090

CMD ["python", "-u", "app.py"]
