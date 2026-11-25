# open-outlook.ps1 — abre um email específico no Outlook clássico a partir de um
# link obsmail:<EntryID>. Usa a API COM do Outlook (GetItemFromID), que abre o
# item exato independentemente de onde o Office está instalado.
#
# Registrado como handler do protocolo obsmail: por install-outlook-link.bat.
param([string]$url)

try {
    $eid = $url -replace '^obsmail:', ''
    $eid = [System.Uri]::UnescapeDataString($eid)
    if ([string]::IsNullOrWhiteSpace($eid)) { return }

    $ol   = New-Object -ComObject Outlook.Application
    $item = $ol.GetNamespace('MAPI').GetItemFromID($eid)
    $item.Display($true)
}
catch {
    # Falha comum: o email foi movido/arquivado (o EntryID muda ao mudar de pasta).
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    [System.Windows.Forms.MessageBox]::Show(
        "Nao foi possivel abrir este email no Outlook.`n`nEle pode ter sido movido ou arquivado (o link e valido enquanto o email ficar na pasta original).`n`nDetalhe: $($_.Exception.Message)",
        "Mordomo do Conhecimento") | Out-Null
}
