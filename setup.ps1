# One-command local setup for Windows PowerShell: venv, deps, schema, seed, run.
#
#   .\setup.ps1                # first run: everything
#   .\setup.ps1 -Start         # skip setup, just start the server
#
# If scripts are blocked:  Set-ExecutionPolicy -Scope Process Bypass

param([switch]$Start)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Green }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# ------------------------------------------------------------ prerequisites
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail "python not found. Install Python 3.11+ from python.org and tick 'Add to PATH'." }
if (-not (Get-Command psql   -ErrorAction SilentlyContinue)) { Fail "psql not found. Add PostgreSQL's bin folder (e.g. C:\Program Files\PostgreSQL\16\bin) to PATH." }

$pyOk = python -c "import sys; print(int(sys.version_info >= (3, 11)))"
if ($pyOk -ne "1") { Fail "Python 3.11+ required." }

$port = if ($env:PORT) { $env:PORT } else { "8000" }

# --------------------------------------------------------------- start only
if ($Start) {
    if (-not (Test-Path .venv)) { Fail "No .venv yet - run .\setup.ps1 without -Start first." }
    Say "Starting on http://localhost:$port"
    & .\.venv\Scripts\uvicorn.exe app.main:app --reload --port $port
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------------- env
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Say "Created .env from .env.example"
}

# Load .env into the process so psql and the seed see DATABASE_URL etc.
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*(#.*)?$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim('"').Trim("'"), "Process")
    }
}
if (-not $env:DATABASE_URL) { Fail "DATABASE_URL is empty in .env" }

# --------------------------------------------------------------------- venv
if (-not (Test-Path .venv)) {
    Say "Creating virtualenv"
    python -m venv .venv
}
Say "Installing dependencies"
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt

# ----------------------------------------------------------------- database
Say "Checking database connection"
& psql $env:DATABASE_URL -Atc "SELECT 1" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Cannot connect to: $env:DATABASE_URL" -ForegroundColor Yellow
    Write-Host "Open 'SQL Shell (psql)' from the Start menu and run:"
    Write-Host "  CREATE ROLE crm LOGIN PASSWORD 'crm' SUPERUSER;"
    Write-Host "  CREATE DATABASE crm OWNER crm;"
    exit 1
}

Say "Applying schema"
& psql $env:DATABASE_URL -v ON_ERROR_STOP=1 -q -f db\schema.sql
if ($LASTEXITCODE -ne 0) { Fail "Schema failed to apply." }

# ------------------------------------------------------------------- seed
$owner = if ($env:OWNER_EMAIL) { $env:OWNER_EMAIL } else { "admin@example.com" }
Say "Seeding workspace (owner: $owner)"
& .\.venv\Scripts\python.exe -m db.seed
if ($LASTEXITCODE -ne 0) { Fail "Seed failed." }

$tenant = if ($env:SEED_TENANT_SLUG) { $env:SEED_TENANT_SLUG } else { "demo" }
Write-Host ""
Write-Host "  Sign in:   http://localhost:$port/login"
Write-Host "  Workspace: $tenant"
Write-Host "  Email:     $owner"
Write-Host "  Password:  (OWNER_PASSWORD from .env)"
Write-Host ""
Write-Host "  Test form: http://localhost:$port/form-example.html"
Write-Host "  API docs:  http://localhost:$port/api/docs"
Write-Host ""

Say "Starting server (Ctrl+C to stop)"
& .\.venv\Scripts\uvicorn.exe app.main:app --reload --port $port
