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
    CAL_FUTURE_DAYS  janela do calendário à frente (padrão 30; 0 desliga reuniões)
    CAL_PAST_DAYS    janela do calendário para trás (padrão 1)

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
MAX_PER_RUN     = int(os.environ.get("MAX_PER_RUN", "100"))  # enfileirar é barato (async no agente)
RATELIMIT_PAUSE = int(os.environ.get("RATELIMIT_PAUSE", "1800"))
_ENV_POLL       = os.environ.get("POLL_SECONDS")       # sobrepõe a config do agente
_ENV_BACKFILL   = os.environ.get("BACKFILL_DAYS")      # sobrepõe a config do agente

OL_INBOX       = 6
OL_CALENDAR    = 9
OL_MAIL        = 43
OL_APPOINTMENT = 26
BODY_MAX       = 20000
HEADERS        = {"X-Api-Key": AGENT_API_KEY, "Content-Type": "application/json"}

# Janela do calendário (reuniões/calls). 0 em CAL_FUTURE_DAYS desliga o calendário.
CAL_FUTURE_DAYS = int(os.environ.get("CAL_FUTURE_DAYS", "30"))
CAL_PAST_DAYS   = int(os.environ.get("CAL_PAST_DAYS", "1"))


def _folder(fid):
    outlook = win32com.client.Dispatch("Outlook.Application")
    return outlook.GetNamespace("MAPI").GetDefaultFolder(fid)


def _inbox():
    return _folder(OL_INBOX)


def _config() -> dict:
    """Lê intervalo/backfill/filtro do agente; env tem prioridade quando definido."""
    poll, backfill, skip = 300, 0, []
    try:
        r = requests.get(f"{AGENT_URL}/api/collector-config", headers=HEADERS, timeout=30)
        if r.status_code == 200:
            d = r.json()
            poll, backfill = int(d.get("poll_seconds", 300)), int(d.get("backfill_days", 0))
            skip = [str(p).lower() for p in d.get("skip_patterns", [])]
    except Exception:
        pass
    if _ENV_POLL:
        poll = int(_ENV_POLL)
    if _ENV_BACKFILL:
        backfill = int(_ENV_BACKFILL)
    return {"poll": max(30, poll), "backfill": max(0, backfill), "skip": skip,
            "cal_future": CAL_FUTURE_DAYS, "cal_past": CAL_PAST_DAYS}


def _skip(sender: str, subject: str, patterns: list) -> bool:
    if not patterns:
        return False
    hay = f"{sender} {subject}".lower()
    return any(p in hay for p in patterns)


def _processed_ids(since_date: str, source: str = "outlook") -> set:
    try:
        r = requests.get(f"{AGENT_URL}/api/processed",
                         params={"source": source, "since": since_date},
                         headers=HEADERS, timeout=60)
        if r.status_code == 200:
            return set(r.json().get("ids", []))
    except Exception as e:
        print("  ⚠ não consegui ler os processados do agente:", e)
    return set()


def _post(endpoint: str, payload: dict) -> str:
    r = requests.post(f"{AGENT_URL}{endpoint}", json=payload, headers=HEADERS, timeout=60)
    if 200 <= r.status_code < 300:
        return "ok"
    print(f"  ❌ agente respondeu {r.status_code}: {r.text[:150]}")
    return "error"


def _send(payload: dict) -> str:
    """'ok' | 'error'. O agente só ENFILEIRA (202) — a destilação é assíncrona no servidor."""
    r = requests.post(f"{AGENT_URL}/api/ingest-email", json=payload, headers=HEADERS, timeout=60)
    if 200 <= r.status_code < 300:
        return "ok"
    print(f"  ❌ agente respondeu {r.status_code}: {r.text[:150]}")
    return "error"


def _cycle(cfg: dict) -> str:
    window = datetime.datetime.now() - datetime.timedelta(days=cfg["backfill"])
    done = _processed_ids(window.strftime("%Y-%m-%d"))
    patterns = cfg.get("skip", [])
    skipped = 0

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
        sender = getattr(item, "SenderName", "") or getattr(item, "SenderEmailAddress", "") or ""
        subject = getattr(item, "Subject", "") or ""
        if _skip(sender, subject, patterns):
            skipped += 1
            continue
        novos.append((item, mid, received))

    novos.reverse()  # mais antigo primeiro
    if not novos:
        print(f"  nenhum email novo." + (f" ({skipped} ignorado(s) pelo filtro)" if skipped else ""))
        return "idle"
    if skipped:
        print(f"  ({skipped} ignorado(s) pelo filtro)")

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
            print("    ❌ falha de rede (retoma no próximo ciclo):", e)
            continue  # idempotente no agente; reenvio é seguro
        print("    ✅ enfileirado" if r == "ok" else "    ⚠ não enfileirado (tenta de novo depois)")

    if len(novos) > MAX_PER_RUN:
        print(f"  … ainda {len(novos) - MAX_PER_RUN} p/ enfileirar; continua no próximo ciclo.")
    return "ok"


def _cycle_calendar(cfg: dict):
    """Lê reuniões/compromissos do Calendário na janela [agora-past, agora+future]
    e envia os novos ao agente (/api/ingest-event)."""
    fut, past = cfg.get("cal_future", 0), cfg.get("cal_past", 1)
    if fut <= 0:
        return
    now = datetime.datetime.now()
    ws, we = now - datetime.timedelta(days=past), now + datetime.timedelta(days=fut)
    done = _processed_ids(ws.strftime("%Y-%m-%d"), source="calendar")

    try:
        items = _folder(OL_CALENDAR).Items
        items.IncludeRecurrences = True           # expande séries recorrentes
        items.Sort("[Start]")
        restrict = "[Start] >= '%s' AND [Start] <= '%s'" % (
            ws.strftime("%m/%d/%Y %I:%M %p"), we.strftime("%m/%d/%Y %I:%M %p"))
        seq = items.Restrict(restrict)
    except Exception as e:
        print("  ⚠ calendário indisponível:", e)
        return

    novos, count = [], 0
    for appt in seq:
        count += 1
        if count > 2000:
            break
        if getattr(appt, "Class", None) != OL_APPOINTMENT:
            continue
        try:
            st = appt.Start
            start = datetime.datetime(st.year, st.month, st.day, st.hour, st.minute)
        except Exception:
            continue
        if start < ws or start > we:
            continue
        gaid = getattr(appt, "GlobalAppointmentID", "") or getattr(appt, "EntryID", "")
        eid = f"{gaid}|{start:%Y-%m-%d %H:%M}"
        if not gaid or eid in done:
            continue
        novos.append((appt, eid, start))

    if not novos:
        print("  nenhuma reunião nova.")
        return
    print(f"  {len(novos)} reunião(ões) nova(s) (enviando até {MAX_PER_RUN}).")
    for appt, eid, start in novos[:MAX_PER_RUN]:
        try:
            en = appt.End
            end = datetime.datetime(en.year, en.month, en.day, en.hour, en.minute).strftime("%Y-%m-%d %H:%M")
        except Exception:
            end = ""
        payload = {
            "subject":   getattr(appt, "Subject", "") or "(sem título)",
            "start":     start.strftime("%Y-%m-%d %H:%M"),
            "end":       end,
            "organizer": getattr(appt, "Organizer", "") or "",
            "location":  getattr(appt, "Location", "") or "",
            "attendees": (getattr(appt, "RequiredAttendees", "") or "")[:500],
            "body":      (getattr(appt, "Body", "") or "")[:BODY_MAX],
            "event_id":  eid,
        }
        print(f"  → {start:%Y-%m-%d %H:%M} {payload['subject'][:55]}")
        try:
            r = _post("/api/ingest-event", payload)
        except Exception as e:
            print("    ❌ falha de rede (retoma depois):", e)
            continue
        print("    ✅ enfileirado" if r == "ok" else "    ⚠ não enfileirado")


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
    if cfg["cal_future"] > 0:
        print(f"Calendário: reuniões de {cfg['cal_past']} dia(s) atrás até {cfg['cal_future']} dias à frente.")
    print("Ctrl+C para parar — retoma no mesmo ponto (o banco do agente é a fonte da verdade).\n")

    while True:
        result = "ok"
        try:
            cfg = _config()   # relê o intervalo a cada ciclo (editável nas Settings)
            print(f"[{datetime.datetime.now():%H:%M:%S}] verificando emails… (intervalo {cfg['poll']}s)")
            result = _cycle(cfg)
            print(f"[{datetime.datetime.now():%H:%M:%S}] verificando calendário…")
            _cycle_calendar(cfg)
        except Exception as e:
            print("  ⚠ erro no ciclo:", e)
        time.sleep(RATELIMIT_PAUSE if result == "ratelimit" else cfg["poll"])


if __name__ == "__main__":
    main()
