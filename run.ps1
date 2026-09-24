$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot
try { $Host.UI.RawUI.WindowTitle = '爆款复制 Harness' } catch {}

if (-not (Test-Path -LiteralPath '.venv')) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3.12 -m venv .venv
    } else {
        python -m venv .venv
    }
}

$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
& $Python -m pip --version *> $null
if ($LASTEXITCODE -ne 0) {
    & $Python -m ensurepip --upgrade
}

& $Python -c 'import fastapi, httpx, dotenv, multipart, uvicorn' *> $null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install -e .
}

& '.\.venv\Scripts\python.exe' -m uvicorn app.main:app --host 127.0.0.1 --port 8788
