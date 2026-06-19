FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download embedding model (multilingual-e5-large: 1024 dims, qualidade superior)
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('intfloat/multilingual-e5-large')"

COPY watcher.py .

CMD ["python", "-u", "watcher.py"]
