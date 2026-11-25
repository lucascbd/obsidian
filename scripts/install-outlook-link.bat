@echo off
REM install-outlook-link.bat — registra o protocolo obsmail: no Windows (uma vez).
REM Clicar em "abrir" no painel passa a abrir o email exato no Outlook classico.
REM Rode este .bat uma vez; deixe o open-outlook.ps1 na MESMA pasta que este arquivo.
REM Nao precisa de admin (registra em HKEY_CURRENT_USER).
setlocal

set "PS1=%~dp0open-outlook.ps1"
if not exist "%PS1%" (
  echo.
  echo ERRO: open-outlook.ps1 nao foi encontrado na mesma pasta deste .bat.
  echo Coloque os dois arquivos juntos e rode de novo.
  echo.
  pause
  exit /b 1
)

set "CMD=powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%PS1%\" \"%%1\""

reg add "HKCU\Software\Classes\obsmail" /ve /d "URL:Obsidian Mail" /f >nul
reg add "HKCU\Software\Classes\obsmail" /v "URL Protocol" /d "" /f >nul
reg add "HKCU\Software\Classes\obsmail\shell\open\command" /ve /d "%CMD%" /f >nul

echo.
echo Protocolo obsmail: registrado com sucesso.
echo Handler: %PS1%
echo.
echo Agora, no painel, o botao "abrir" do to-do abre o email no Outlook classico.
echo (Na primeira vez o navegador pode perguntar se permite abrir o aplicativo.)
echo.
pause
