# Start the control tower UI and everything behind it (see scripts/dev.py for the steps).
#   .\scripts\dev.ps1            keep the staged morning if there is one
#   .\scripts\dev.ps1 -Stage     restage the morning (no model calls)
#   .\scripts\dev.ps1 -Fresh     rerun the milestone 1 scenario, then stage
#   .\scripts\dev.ps1 -Prod      next build + next start instead of next dev
#   .\scripts\dev.ps1 -NoLlm     no Ollama call while starting (staging without embeddings)
param([switch]$Stage, [switch]$Fresh, [switch]$Prod, [switch]$NoWeb, [switch]$NoLlm)
$python = if ($env:CT_PYTHON) { $env:CT_PYTHON } else { "python" }   # the project venv's python
$argv = @()
if ($Stage) { $argv += "--stage" }
if ($Fresh) { $argv += "--fresh" }
if ($Prod) { $argv += "--prod" }
if ($NoWeb) { $argv += "--no-web" }
if ($NoLlm) { $argv += "--no-llm" }
Push-Location (Split-Path -Parent $PSScriptRoot)
try { & $python "$PSScriptRoot\dev.py" @argv } finally { Pop-Location }
