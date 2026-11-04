#!/usr/bin/env python3
"""
outlook_collector.py — coleta emails novos do Outlook LOCAL e envia ao agente.

Roda no PC do trabalho (Windows, Outlook desktop). O registro do que já foi
processado vive no AGENTE (SQLite), não aqui — o coletor é sem estado:
  1. pergunta ao agente quais emails já foram processados (/api/processed);
  2. lê a Caixa de Entrada na janela de backfill;
  3. envia só os que ainda não estão no banco do agente (/api/ingest-email).

O intervalo de verificação é configurável nas Settings do agente (/api/collector-config).
Se o agente sinalizar limite de uso do LLM (429), pausa e retoma — o próprio banco
do agente garante que nada é reprocessado.

CONFIGURAÇÃO (variáveis de ambiente):
    AGENT_URL        ex.: https://seu-agente.exemplo.com     (obrigatório)
    AGENT_API_KEY    a mesma chave configurada no agente     (obrigatório)
    MAX_PER_RUN      máx. de emails por ciclo (padrão 10)
    RATELIMIT_PAUSE  pausa no limite do LLM, em seg. (padrão 1800)
    BACKFILL_DAYS    varredura inicial em dias; sobrepõe a config do agente se definido
    POLL_SECONDS     intervalo; sobrepõe a config do agente se definido

USO:
    pip install pywin32 requests truststore
    set AGENT_URL=https://seu-agente...
    set AGENT_API_KEY=xxxxxxxx
    python outlook_collector.py
"""

import os
import sys
import time
import datetime

# TLS da rede corporativa (MITM) → usa o trust store do Windows.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

try:
    import win32com.client
    import pywintypes
    import requests
except ImportError:
    print("Faltam dependências. Rode:  pip install pywin32 requests truststore")
    sys.exit(1)

AGENT_URL       = os.environ.get("AGENT_URL", "").rstrip("/")
AGENT_API_KEY   = os.environ.get("AGENT_API_KEY", "")
MAX_PER_RUN     = int(os.environ.get("MAX_PER_RUN", "10"))
RATELIMIT_PAUSE = int(os.environ.get("RATELIMIT_PAUSE", "1800"))
_ENV_POLL       = os.environ.get("POLL_SECONDS")       # sobrepõe a config do agente
_ENV_BACKFILL   = os.environ.get("BACKFILL_DAYS")      # sobrepõe a config do agente

OL_INBOX = 6
OL_MAIL  = 43
BODY_MAX = 20000
HEADERS  = {"X-Api-Key": AGENT_API_KEY, "Content-Type": "application/json"}


def _inbox():
    outlook = win32com.client.Dispatch("Outlook.Application")
    ns = outlook.GetNamespace("MAPI")
    return ns.GetDefaultFolder(OL_INBOX)


def _config() -> dict:
    """Lê intervalo/backfill do agente; env tem prioridade quando definido."""
    poll, backfill = 300, 0
    try:
        r = requests.get(f"{AGENT_URL}/api/collector-config", headers=HEADERS, timeout=30)
        if r.status_code == 200:
            d = r.json()
            poll, backfill = int(d.get("poll_seconds", 300)), int(d.get("backfill_days", 0))
    except Exception:
        pass
    if _ENV_POLL:
        poll = int(_ENV_POLL)
    if _ENV_BACKFILL:
        backfill = int(_ENV_BACKFILL)
    return {"poll": max(30, poll), "backfill": max(0, backfill)}


def _processed_ids(since_date: str) -> set:
    try:
        r = requests.get(f"{AGENT_URL}/api/processed",
                         params={"source": "outlook", "since": since_date},
                         headers=HEADERS, timeout=60)
        if r.status_code == 200:
            return set(r.json().get("ids", []))
    except Exception as e:
        print("  ⚠ não consegui ler os processados do agente:", e)
    return set()


def _send(payload: dict) -> str:
    """'ok' | 'ratelimit' | 'error'."""
    r = requests.post(f"{AGENT_URL}/api/ingest-email", json=payload, headers=HEADERS, timeout=180)
    if r.status_code == 200:
        return "ok"
    if r.status_code == 429:
        return "ratelimit"
    print(f"  ❌ agente respondeu {r.status_code}: {r.text[:200]}")
    return "error"


def _cycle(backfill_days: int) -> str:
    window = datetime.datetime.now() - datetime.timedelta(days=backfill_days)
    done = _processed_ids(window.strftime("%Y-%m-%d"))

    try:
        items = _inbox().Items
    except pywintypes.com_error as e:
        print("  ❌ Outlook indisponível:", e)
        return "idle"

    try:
        items.Sort("[ReceivedTime]", True)  # novos primeiro
    except Exception:
        pass

    novos = []
    for item in items:
        if getattr(item, "Class", None) != OL_MAIL:
            continue
        try:
            rt = item.ReceivedTime
            received = datetime.datetime(rt.year, rt.month, rt.day, rt.hour, rt.minute, rt.second)
        except Exception:
            continue
        if received < window:
            break
        mid = getattr(item, "InternetMessageID", "") or getattr(item, "EntryID", "")
        if not mid or mid in done:
            continue
        novos.append((item, mid, received))

    novos.reverse()  # mais antigo primeiro
    if not novos:
        print("  nenhum email novo.")
        return "idle"

    print(f"  {len(novos)} email(s) na fila (processando até {MAX_PER_RUN}).")
    for item, mid, received in novos[:MAX_PER_RUN]:
        subject = getattr(item, "Subject", "") or "(sem assunto)"
        sender = getattr(item, "SenderName", "") or getattr(item, "SenderEmailAddress", "") or "?"
        body = (getattr(item, "Body", "") or "")[:BODY_MAX]
        payload = {"subject": subject, "from": sender,
                   "date": received.strftime("%Y-%m-%d %H:%M"), "body": body, "msg_id": mid}
        print(f"  → {received:%Y-%m-%d %H:%M} [{sender}] {subject[:55]}")
        try:
            r = _send(payload)
        except Exception as e:
            print("    ❌ falha de rede (retoma depois):", e)
            return "idle"  # tenta tudo de novo no próximo ciclo
        if r == "ratelimit":
            print(f"    ⏸ limite de uso do LLM. Pausando ~{RATELIMIT_PAUSE // 60} min e retomando daqui.")
            return "ratelimit"
        print("    ✅ ok" if r == "ok" else "    ⚠ erro (registrado; não reprocessa).")

    if len(novos) > MAX_PER_RUN:
        print(f"  … ainda {len(novos) - MAX_PER_RUN} na fila; continua no próximo ciclo.")
    return "ok"


def main():
    if not AGENT_URL or not AGENT_API_KEY:
        print("Configure AGENT_URL e AGENT_API_KEY. Ex.:")
        print("  set AGENT_URL=https://seu-agente...")
        print("  set AGENT_API_KEY=xxxxxxxx")
        sys.exit(1)

    print(f"Coletor de email iniciado. Agente: {AGENT_URL}")
    try:
        h = requests.get(f"{AGENT_URL}/health", timeout=15)
        print(f"Conexão com o agente: {'OK' if h.status_code == 200 else 'resposta ' + str(h.status_code)}")
    except Exception as e:
        print(f"⚠ Não consegui falar com o agente ({AGENT_URL}): {e}")

    cfg = _config()
    if cfg["backfill"] > 0:
        print(f"Varredura inicial: últimos {cfg['backfill']} dias (resumível via banco do agente).")
    print("Ctrl+C para parar — retoma no mesmo ponto (o banco do agente é a fonte da verdade).\n")

    while True:
        result = "ok"
        try:
            cfg = _config()   # relê o intervalo a cada ciclo (editável nas Settings)
            print(f"[{datetime.datetime.now():%H:%M:%S}] verificando emails… (intervalo {cfg['poll']}s)")
            result = _cycle(cfg["backfill"])
        except Exception as e:
            print("  ⚠ erro no ciclo:", e)
        time.sleep(RATELIMIT_PAUSE if result == "ratelimit" else cfg["poll"])


if __name__ == "__main__":
    main()
