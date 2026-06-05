# Thin wrapper: forwards to scripts/evi_tools.py with the venv/python on PATH.
#   .\scripts\evi-tools.ps1 list
#   .\scripts\evi-tools.ps1 install ffmpeg
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = if (Test-Path "$here\..\.venv\Scripts\python.exe") { "$here\..\.venv\Scripts\python.exe" } else { "python" }
& $py "$here\evi_tools.py" @args
