#!/usr/bin/env python3
"""
outlook_probe.py — lê seus últimos emails do Outlook LOCAL (sem Graph/app/consentimento).

Usa a automação COM do Outlook desktop (que já tem sua caixa sincronizada), como
você, no PC gerenciado. Não envia nada pra lugar nenhum — só lê a Caixa de
Entrada e imprime na tela, pra provar que dá pra coletar email sem depender do
admin.

USO (no PC do trabalho, com o Outlook instalado e configurado):
    pip install pywin32
    python outlook_probe.py

Observações:
- Só funciona no Windows com o Outlook desktop (clássico) instalado.
- Pode aparecer um aviso de "um programa está tentando acessar o Outlook" —
  é a proteção de acesso programático; permita por alguns minutos para o teste.
"""

import sys

try:
    import win32com.client  # parte do pacote pywin32
    import pywintypes
except ImportError:
    print("Falta dependência. Rode:  pip install pywin32")
    sys.exit(1)

N = 5  # quantos emails recentes mostrar

# Pastas padrão do Outlook (olDefaultFolders)
OL_INBOX = 6


def _snippet(item) -> str:
    try:
        body = (item.Body or "").strip().replace("\r", " ").replace("\n", " ")
        return body[:120]
    except Exception:
        return "(corpo indisponível)"


def main():
    print("Outlook local — teste de leitura de email (sem Graph)\n")
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        ns = outlook.GetNamespace("MAPI")
    except pywintypes.com_error as e:
        print("❌ Não consegui falar com o Outlook. Ele está instalado e aberto?")
        print("   detalhe:", e)
        sys.exit(1)

    try:
        user = ns.CurrentUser
        print(f"Conta: {getattr(user, 'Name', '?')}")
    except Exception:
        pass

    try:
        inbox = ns.GetDefaultFolder(OL_INBOX)
    except Exception as e:
        print("❌ Não consegui abrir a Caixa de Entrada:", e)
        sys.exit(1)

    items = inbox.Items
    try:
        items.Sort("[ReceivedTime]", True)  # mais recentes primeiro
    except Exception:
        pass

    print(f"\n── {N} emails mais recentes da Caixa de Entrada ──")
    shown = 0
    for item in items:
        # Só itens de email (olMail = 43); pula convites/outros
        if getattr(item, "Class", None) != 43:
            continue
        try:
            when = item.ReceivedTime.strftime("%Y-%m-%d %H:%M")
        except Exception:
            when = "?"
        sender = getattr(item, "SenderName", "") or getattr(item, "SenderEmailAddress", "") or "?"
        subject = getattr(item, "Subject", "") or "(sem assunto)"
        print(f"\n• {when}  [{sender}]")
        print(f"  {subject[:80]}")
        print(f"  {_snippet(item)}")
        shown += 1
        if shown >= N:
            break

    if shown == 0:
        print("  (nenhum email de leitura encontrado — caixa vazia ou só convites?)")

    print("\n" + "=" * 60)
    print(f"✅ Leu {shown} email(s) localmente, sem Graph nem consentimento.")
    print("Me diz se apareceu a lista certa. Se sim, dá pra montar o coletor de")
    print("email em cima disso enquanto o Teams aguarda o admin.")
    print("=" * 60)


if __name__ == "__main__":
    main()
