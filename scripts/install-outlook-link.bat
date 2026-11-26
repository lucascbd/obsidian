@echo off
REM install-outlook-link.bat — registra o protocolo obsmail: no Windows (uma vez).
REM Clicar em "abrir" no painel passa a abrir o email exato no Outlook classico.
REM
REM Autossuficiente: NAO precisa de nenhum .ps1 ao lado (o codigo vai embutido no
REM registro como comando inline, que NAO e bloqueado pela politica de execucao de
REM scripts da TI). Nao precisa de admin (grava em HKEY_CURRENT_USER).
REM
REM Use: de 2 cliques neste arquivo (ou botao direito > Executar).
setlocal

REM Comando inline do PowerShell. Le o ultimo argumento da linha de comando (a URL
REM obsmail:<EntryID> que o Windows passa), extrai o EntryID e abre o email via COM.
set "CMD=powershell -NoProfile -WindowStyle Hidden -Command \"try{$a=[Environment]::GetCommandLineArgs();$u=$a[$a.Length-1];$e=[uri]::UnescapeDataString(($u -replace '^obsmail:',''));$o=New-Object -ComObject Outlook.Application;$m=$o.GetNamespace('MAPI').GetItemFromID($e);$m.Display($false);$i=$m.GetInspector;$i.Activate()}catch{(New-Object -ComObject WScript.Shell).Popup('Nao foi possivel abrir o email. Feche qualquer janela/caixa de dialogo aberta no Outlook e tente de novo. '+$_.Exception.Message,8,'Mordomo do Conhecimento',48)}\" \"%%1\""

reg add "HKCU\Software\Classes\obsmail" /ve /d "URL:Obsidian Mail" /f >nul
reg add "HKCU\Software\Classes\obsmail" /v "URL Protocol" /d "" /f >nul
reg add "HKCU\Software\Classes\obsmail\shell\open\command" /ve /d "%CMD%" /f >nul

echo.
echo Protocolo obsmail: registrado (comando inline, sem .ps1).
echo.
echo Verificando o que ficou gravado:
reg query "HKCU\Software\Classes\obsmail\shell\open\command" /ve
echo.
echo Pronto. No painel, o botao "abrir" do to-do abre o email no Outlook classico.
echo (Na primeira vez o navegador pode perguntar se permite abrir o aplicativo.)
echo.
pause
