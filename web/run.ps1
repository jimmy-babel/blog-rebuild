param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$webRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $webRoot
if (-not (Test-Path -LiteralPath (Join-Path $webRoot '.env'))) {
  Copy-Item -LiteralPath (Join-Path $webRoot '.env.example') -Destination (Join-Path $webRoot '.env')
  Write-Host 'Created web/.env. Fill in OPENAI_API_KEY and OPENAI_BASE_URL, then restart.' -ForegroundColor Yellow
}
python -m uvicorn app:app --host 127.0.0.1 --port $Port
