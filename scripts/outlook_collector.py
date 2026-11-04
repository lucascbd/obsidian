#!/usr/bin/env python3
"""
outlook_collector.py — coleta emails novos do Outlook LOCAL e envia ao agente.

Roda no PC do trabalho (Windows, Outlook desktop). A cada ciclo, lê a Caixa de
Entrada, pega os emails NOVOS (recebidos depois que o coletor começou e ainda
não enviados) e faz POST do texto para o agente, que destila em nota no vault.

NÃO envia nada além do agente que VOCÊ configurar. Só leitura do Outlook.

CONFIGURAÇÃO (variáveis de ambiente):
    AGENT_URL       ex.: https://seu-agente.exemplo.com     (obrigatório)
    AGENT_API_KEY   a mesma chave configurada no agente     (obrigatório)
    POLL_SECONDS    intervalo entre ciclos (padrão 300)
    MAX_PER_RUN     máx. de emails por ciclo (padrão 10)
    BACKFILL_DAYS   varredura inicial p/ trás, em dias (0 = só daqui pra frente; 120 = ~4 meses)
    RATELIMIT_PAUSE pausa quando o LLM bate o limite de uso, em seg. (padrão 1800)

USO:
    pip install pywin32 requests truststore
    set AGENT_URL=https://seu-agente...
    set AGENT_API_KEY=xxxxxxxx
    python outlook_collector.py
"""

import os
import sys
import json
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

AGENT_URL    = os.environ.get("AGENT_URL", "").rstrip("/")
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
MAX_PER_RUN  = int(os.environ.get("MAX_PER_RUN", "10"))
# Varredura inicial: quantos dias para trás processar na primeira execução
# (0 = só daqui pra frente; 120 = ~4 meses). Depois é só incremental.
BACKFILL_DAYS   = int(os.environ.get("BACKFILL_DAYS", "0"))
# Pausa quando o agente sinaliza limite de uso do LLM (429). Padrão 30 min.
RATELIMIT_PAUSE = int(os.environ.get("RATELIMIT_PAUSE", "1800"))

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outlook_collector_state.json")
OL_INBOX   = 6
OL_MAIL    = 43
BODY_MAX   = 20000


def _load_state() -> dict:
    try:
        return json.load(open(STATE_FILE, "r", encoding="utf-8"))
    except Exception:
        # Primeira execução: janela = agora - BACKFILL_DAYS (0 = só daqui pra frente).
        start = datetime.datetime.now() - datetime.timedelta(days=BACKFILL_DAYS)
        return {"start_iso": start.isoformat(), "processed": []}


def _save_state(st: dict) -> None:
    try:
        st["processed"] = st["processed"][-5000:]  # não deixar crescer sem limite
        json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as e:
        print("  (aviso: não consegui salvar o estado:", e, ")")


def _inbox():
    outlook = win32com.client.Dispatch("Outlook.Application")
    ns = outlook.GetNamespace("MAPI")
    return ns.GetDefaultFolder(OL_INBOX)


def _send(payload: dict) -> str:
    """Retorna 'ok', 'ratelimit' (429 = limite de uso do LLM) ou 'error'."""
    r = requests.post(
        f"{AGENT_URL}/api/ingest-email",
        json=payload,
        headers={"X-Api-Key": AGENT_API_KEY, "Content-Type": "application/json"},
        timeout=180,
    )
    if r.status_code == 200:
        return "ok"
    if r.status_code == 429:
        return "ratelimit"
    print(f"  ❌ agente respondeu {r.status_code}: {r.text[:200]}")
    return "error"


def _cycle(state: dict):
    start = datetime.datetime.fromisoformat(state["start_iso"])
    processed = set(state["processed"])

    try:
        items = _inbox().Items
    except pywintypes.com_error as e:
        print("  ❌ Outlook indisponível:", e)
        return

    try:
        items.Sort("[ReceivedTime]", True)  # novos primeiro
    except Exception:
        pass

    novos = []
    for item in items:
        if getattr(item, "Class", None) != OL_MAIL:
            continue
        try:
            received = item.ReceivedTime
            received = datetime.datetime(received.year, received.month, received.day,
                                         received.hour, received.minute, received.second)
        except Exception:
            continue
        if received < start:
            break  # como está ordenado desc, tudo a seguir é antigo
        mid = getattr(item, "InternetMessageID", "") or getattr(item, "EntryID", "")
        if not mid or mid in processed:
            continue
        novos.append((item, mid, received))

    novos.reverse()  # processa do mais antigo p/ o mais novo
    if not novos:
        print("  nenhum email novo (aguardando próximo ciclo).")
        return "idle"

    restantes = len(novos)
    print(f"  {restantes} email(s) na fila (processando até {MAX_PER_RUN} agora).")

    for item, mid, received in novos[:MAX_PER_RUN]:
        subject = getattr(item, "Subject", "") or "(sem assunto)"
        sender = getattr(item, "SenderName", "") or getattr(item, "SenderEmailAddress", "") or "?"
        body = (getattr(item, "Body", "") or "")[:BODY_MAX]
        payload = {
            "subject": subject,
            "from": sender,
            "date": received.strftime("%Y-%m-%d %H:%M"),
            "body": body,
            "msg_id": mid,
        }
        print(f"  → {received:%Y-%m-%d %H:%M} [{sender}] {subject[:55]}")
        try:
            r = _send(payload)
        except Exception as e:
            print("    ❌ falha de rede (tenta de novo depois):", e)
            continue  # não marca: reprocessa no próximo ciclo
        if r == "ratelimit":
            print(f"    ⏸ limite de uso do LLM. Pausando ~{RATELIMIT_PAUSE//60} min e retomando deste ponto.")
            _save_state(state)
            return "ratelimit"   # NÃO marca este email: ele é o próximo quando retomar
        # 'ok' ou 'error': marca como processado (erro não-limite não deve travar a fila)
        processed.add(mid)
        state["processed"].append(mid)
        _save_state(state)       # persiste a CADA email → retomável no ponto exato
        print("    ✅ ok" if r == "ok" else "    ⚠ pulado (erro não-limite)")

    if restantes > MAX_PER_RUN:
        print(f"  … ainda {restantes - MAX_PER_RUN} na fila; continua no próximo ciclo.")
    return "ok"


def main():
    if not AGENT_URL or not AGENT_API_KEY:
        print("Configure AGENT_URL e AGENT_API_KEY antes de rodar. Ex.:")
        print("  set AGENT_URL=https://seu-agente...")
        print("  set AGENT_API_KEY=xxxxxxxx")
        sys.exit(1)

    print(f"Coletor de email iniciado. Agente: {AGENT_URL}  | ciclo: {POLL_SECONDS}s")
    if BACKFILL_DAYS > 0:
        print(f"Varredura inicial: últimos {BACKFILL_DAYS} dias (resumível). Ctrl+C para parar — retoma no mesmo ponto.\n")
    else:
        print("Processa apenas emails recebidos a partir de agora. Ctrl+C para parar.\n")

    # Teste de conexão com o agente (reachability + TLS).
    try:
        h = requests.get(f"{AGENT_URL}/health", timeout=15)
        print(f"Conexão com o agente: {'OK' if h.status_code == 200 else 'resposta ' + str(h.status_code)}\n")
    except Exception as e:
        print(f"⚠ Não consegui falar com o agente ({AGENT_URL}): {e}")
        print("  Confira a URL, se o agente está exposto e o TLS. Vou tentar mesmo assim.\n")
    state = _load_state()
    _save_state(state)  # fixa start_iso na 1ª vez

    while True:
        result = "ok"
        try:
            print(f"[{datetime.datetime.now():%H:%M:%S}] verificando emails…")
            result = _cycle(state)
        except Exception as e:
            print("  ⚠ erro no ciclo:", e)
        # Se bateu o limite do LLM, espera mais tempo (janela do Pro renovar); senão o ciclo normal.
        wait = RATELIMIT_PAUSE if result == "ratelimit" else POLL_SECONDS
        time.sleep(wait)


if __name__ == "__main__":
    main()
