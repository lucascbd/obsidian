#!/usr/bin/env python3
"""
graph_probe.py — teste de acesso delegado ao Microsoft Graph SEM registrar app.

Roda no SEU PC do trabalho (dispositivo gerenciado). Usa o client público da
própria Microsoft ("Microsoft Graph Command Line Tools") + device code flow, com
o SEU próprio acesso. Não cria app, não envia nada pra lugar nenhum — só lê e
imprime na tela se o tenant permite você autenticar e ler email/Teams.

USO:
    pip install msal requests
    python graph_probe.py

Vai aparecer uma URL + um código: abra no navegador, entre com a conta do
trabalho e autorize. O script então testa /me, 5 emails e 1 chat do Teams.

Se algo for bloqueado (consentimento de usuário desligado, conditional access,
etc.), a mensagem de erro aparece — é isso que a gente quer descobrir.
"""

import os
import sys
import json

try:
    import msal
    import requests
except ImportError:
    print("Faltam dependências. Rode:  pip install msal requests")
    sys.exit(1)

# Client público da Microsoft (Microsoft Graph Command Line Tools). NÃO é um app seu.
CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
AUTHORITY = "https://login.microsoftonline.com/organizations"
SCOPES    = ["User.Read", "Mail.Read", "Chat.Read"]   # tudo delegado, só leitura
GRAPH     = "https://graph.microsoft.com/v1.0"
CACHE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "graph_probe_cache.bin")


def _auth() -> str:
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE):
        try:
            cache.deserialize(open(CACHE, "r").read())
        except Exception:
            pass

    app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)

    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            print("❌ Falha ao iniciar o device flow (o tenant pode bloquear este client):")
            print(json.dumps(flow, indent=2, ensure_ascii=False))
            sys.exit(1)
        print("\n" + "=" * 60)
        print(flow["message"])   # "To sign in, use a web browser to open ... and enter the code ..."
        print("=" * 60 + "\n")
        result = app.acquire_token_by_device_flow(flow)   # bloqueia até você autorizar

    if cache.has_state_changed:
        try:
            open(CACHE, "w").write(cache.serialize())
        except Exception:
            pass

    if "access_token" not in result:
        print("❌ Autenticação/consentimento NEGADO:")
        print("   error:", result.get("error"))
        print("   desc :", (result.get("error_description") or "")[:600])
        print("\nProvável causa: consentimento de usuário desligado no tenant, ou")
        print("conditional access bloqueando. É exatamente o que precisávamos saber.")
        sys.exit(1)

    print("✅ Autenticado com sucesso.")
    print("   Scopes concedidos:", result.get("scope", "?"))
    return result["access_token"]


def _get(token: str, path: str):
    r = requests.get(f"{GRAPH}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    body = {}
    try:
        body = r.json()
    except Exception:
        pass
    return r.status_code, body


def _probe(token: str, label: str, path: str):
    print(f"\n── {label} ──")
    print(f"   GET {path}")
    code, body = _get(token, path)
    if code == 200:
        print(f"   ✅ 200 OK")
        return body
    err = (body.get("error") or {})
    print(f"   ❌ {code} — {err.get('code','')}: {(err.get('message') or '')[:300]}")
    return None


def main():
    print("Microsoft Graph — teste de acesso delegado (sem registrar app)")
    token = _auth()

    # 1) Identidade
    me = _probe(token, "Identidade (/me)", "/me")
    if me:
        print(f"      Você: {me.get('displayName')}  <{me.get('userPrincipalName')}>")

    # 2) Emails recentes
    msgs = _probe(token, "Emails (5 mais recentes)",
                  "/me/messages?$top=5&$select=subject,from,receivedDateTime")
    if msgs:
        for m in msgs.get("value", []):
            frm = (((m.get("from") or {}).get("emailAddress") or {}).get("address")) or "?"
            print(f"      • {m.get('receivedDateTime','')[:16]}  [{frm}]  {m.get('subject','(sem assunto)')[:70]}")

    # 3) Chats do Teams
    chats = _probe(token, "Chats do Teams (5 mais recentes)", "/me/chats?$top=5")
    if chats:
        vals = chats.get("value", [])
        print(f"      {len(vals)} chat(s) listado(s).")
        if vals:
            cid = vals[0].get("id")
            cmsgs = _probe(token, "Mensagens do 1º chat (3)",
                           f"/me/chats/{cid}/messages?$top=3")
            if cmsgs:
                for m in cmsgs.get("value", []):
                    body = ((m.get("body") or {}).get("content") or "").strip().replace("\n", " ")
                    frm = (((m.get("from") or {}).get("user") or {}).get("displayName")) or "?"
                    print(f"      • [{frm}] {body[:70]}")

    print("\n" + "=" * 60)
    print("Fim. Me mande o que apareceu (os ✅/❌ de cada bloco) pra decidirmos o próximo passo.")
    print("=" * 60)


if __name__ == "__main__":
    main()
