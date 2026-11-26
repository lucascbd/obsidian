"""embedder.py — embeddings com UM único dono do modelo por host.

O modelo ONNX é a maior fatia de memória do sistema (o e5-large fp32 ocupa ~2,2 GB).
Antes, agent e watcher carregavam cada um a sua cópia. Agora:

- Modo LOCAL (agent): carrega o modelo só quando precisa, com batch pequeno,
  threads limitadas e sem o arena allocator do ONNX Runtime; descarrega depois de
  EMBED_IDLE_SECONDS sem uso (del + gc + malloc_trim devolvem a memória ao SO).
- Modo REMOTO (watcher): com EMBED_REMOTE_URL definido, pede os vetores ao agent
  via HTTP (/internal/embed) e nunca importa o fastembed nem carrega o modelo.

Variáveis de ambiente:
  EMBEDDING_MODEL          modelo (ver PRESETS abaixo para os customizados)
  EMBED_REMOTE_URL         "agent" (= http://127.0.0.1:$AGENT_PORT/internal/embed) ou URL
  EMBED_THREADS            threads do ONNX Runtime (padrão 2)
  EMBED_BATCH              textos por lote de inferência (padrão 8)
  EMBED_IDLE_SECONDS       descarrega o modelo após N s ocioso (padrão 600; 0 = nunca)
"""

import gc
import os
import time
import logging
import threading

from .config import EMBEDDING_MODEL

log = logging.getLogger(__name__)

THREADS = int(os.environ.get("EMBED_THREADS", "2"))
BATCH = int(os.environ.get("EMBED_BATCH", "8"))
IDLE_SECONDS = int(os.environ.get("EMBED_IDLE_SECONDS", "600"))
REMOTE_CHUNK = 64          # textos por requisição HTTP ao agent
MAX_TEXTS_PER_REQUEST = 256

# Modelos que não estão no catálogo do fastembed, registrados como custom.
# e5-small int8 (Xenova): multilíngue, 384 dim, 512 tokens, ~120 MB em disco.
PRESETS = {
    "Xenova/multilingual-e5-small": {
        "hf": "Xenova/multilingual-e5-small",
        "model_file": "onnx/model_quantized.onnx",
        "dim": 384,
        "pooling": "MEAN",
        "normalization": True,
    },
}


class EmbedError(RuntimeError):
    """Falha ao obter embeddings (agent fora do ar, chave errada, etc.)."""


def remote_url() -> str:
    url = os.environ.get("EMBED_REMOTE_URL", "").strip()
    if url.lower() == "agent":
        return f"http://127.0.0.1:{os.environ.get('AGENT_PORT', '8090')}/internal/embed"
    return url


# ── Modo local ──────────────────────────────────────────────────────────────────

_model = None
_lock = threading.Lock()
_last_use = 0.0
_reaper_started = False


def _register_preset(name: str) -> None:
    p = PRESETS.get(name)
    if not p:
        return
    from fastembed import TextEmbedding
    from fastembed.common.model_description import PoolingType, ModelSource
    try:
        TextEmbedding.add_custom_model(
            model=name,
            pooling=getattr(PoolingType, p["pooling"]),
            normalization=p["normalization"],
            sources=ModelSource(hf=p["hf"]),
            dim=p["dim"],
            model_file=p["model_file"],
        )
    except ValueError:
        pass  # já registrado neste processo


def _load():
    from fastembed import TextEmbedding
    _register_preset(EMBEDDING_MODEL)
    log.info(f"[embed] carregando {EMBEDDING_MODEL} (threads={THREADS}, batch={BATCH})")
    t = time.time()
    try:
        m = TextEmbedding(EMBEDDING_MODEL, threads=THREADS, enable_cpu_mem_arena=False)
    except TypeError:  # fastembed antigo sem a opção do arena
        m = TextEmbedding(EMBEDDING_MODEL, threads=THREADS)
    log.info(f"[embed] modelo pronto em {time.time() - t:.1f}s")
    return m


def _malloc_trim() -> None:
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _reaper() -> None:
    global _model
    while True:
        time.sleep(30)
        if IDLE_SECONDS <= 0:
            continue
        with _lock:
            if _model is not None and time.time() - _last_use > IDLE_SECONDS:
                _model = None
                gc.collect()
                _malloc_trim()
                log.info(f"[embed] modelo descarregado após {IDLE_SECONDS}s ocioso")


def embed_local(texts: list) -> list:
    """Embeda no próprio processo (só o agent deve usar)."""
    global _model, _last_use, _reaper_started
    with _lock:
        if _model is None:
            _model = _load()
            if not _reaper_started:
                threading.Thread(target=_reaper, daemon=True, name="embed-reaper").start()
                _reaper_started = True
        out = [v.tolist() for v in _model.embed(texts, batch_size=BATCH)]
        _last_use = time.time()
    return out


def is_loaded() -> bool:
    return _model is not None


# ── Modo remoto ─────────────────────────────────────────────────────────────────

def _embed_remote(texts: list) -> list:
    import requests
    url = remote_url()
    headers = {"X-Api-Key": os.environ.get("AGENT_API_KEY", "")}
    out = []
    for i in range(0, len(texts), REMOTE_CHUNK):
        batch = texts[i:i + REMOTE_CHUNK]
        delay, err = 2, ""
        for _ in range(6):  # ~90 s no total: cobre um restart do agent
            try:
                r = requests.post(url, json={"texts": batch}, headers=headers, timeout=300)
                if r.status_code == 200:
                    out.extend(r.json()["embeddings"])
                    break
                err = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code in (400, 401, 403):
                    raise EmbedError(err)  # não adianta repetir
            except requests.RequestException as e:
                err = str(e)
            time.sleep(delay)
            delay = min(delay * 2, 30)
        else:
            raise EmbedError(f"agent indisponível para embeddings ({url}): {err}")
    return out


# ── API pública ─────────────────────────────────────────────────────────────────

def embed(texts) -> list:
    """Lista de textos → lista de vetores (listas de float)."""
    texts = [t if isinstance(t, str) else str(t) for t in texts]
    if not texts:
        return []
    if remote_url():
        return _embed_remote(texts)
    return embed_local(texts)


def embed_one(text: str) -> list:
    return embed([text])[0]
