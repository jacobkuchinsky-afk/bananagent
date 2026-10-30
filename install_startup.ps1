# Adds (or removes) BananaGent from the per-user Startup folder, so it launches
# at login. No admin rights needed and nothing outside your own account changes.
#
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Remove
#
# It runs pythonw.exe rather than python.exe so no console window appears;
# output goes to bananagent.log in this folder instead.

param([switch]$Remove)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$link = Join-Path ([Environment]::GetFolderPath('Startup')) "BananaGent.lnk"

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; "removed $link" }
    else { "nothing to remove - $link doesn't exist" }
    return
}

$pyw = Join-Path $here ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { throw "missing $pyw - create the venv first (see README)" }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $pyw
$sc.Arguments = "main.py"
$sc.WorkingDirectory = $here
$sc.WindowStyle = 7           # minimized, though pythonw shows nothing anyway
$sc.Description = "BananaGent assistant"
$sc.Save()

"installed $link"
"  -> $pyw main.py  (in $here)"
