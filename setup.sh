#!/usr/bin/env bash
# One-command local setup: venv, dependencies, schema, seed, run.
#
#   ./setup.sh                 # first run: everything
#   ./setup.sh --start         # skip setup, just start the server
#
# Idempotent — safe to re-run. Stops at the first failure and says why.

set -euo pipefail

cd "$(dirname "$0")"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------ prerequisites
command -v python3 >/dev/null || fail "python3 not found. Install Python 3.11 or newer."
command -v psql    >/dev/null || fail "psql not found. Install PostgreSQL client tools (postgresql-client)."

PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 11)))')
[ "$PY_OK" = "1" ] || fail "Python 3.11+ required, found $(python3 --version)."

# --------------------------------------------------------------- start only
if [ "${1:-}" = "--start" ]; then
  [ -d .venv ] || fail "No .venv yet — run ./setup.sh without --start first."
  say "Starting on http://localhost:${PORT:-8000}"
  exec .venv/bin/uvicorn app.main:app --reload --port "${PORT:-8000}"
fi

# ---------------------------------------------------------------------- env
if [ ! -f .env ]; then
  cp .env.example .env
  say "Created .env from .env.example"
fi

# Read .env without `source`: unquoted values with spaces would otherwise
# be executed as commands. Strips quotes and inline comments.
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%#*}"                       # drop comments
  [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]] || continue
  key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"
  val="${val#"${val%%[![:space:]]*}"}"; val="${val%"${val##*[![:space:]]}"}"   # trim
  val="${val#\"}"; val="${val%\"}"; val="${val#\'}"; val="${val%\'}"          # unquote
  export "$key=$val"
done < .env
[ -n "${DATABASE_URL:-}" ] || fail "DATABASE_URL is empty in .env"

# --------------------------------------------------------------------- venv
if [ ! -d .venv ]; then
  say "Creating virtualenv"
  python3 -m venv .venv
fi
say "Installing dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# ----------------------------------------------------------------- database
say "Checking database connection"
if ! psql "$DATABASE_URL" -Atc 'SELECT 1' >/dev/null 2>&1; then
  cat >&2 <<EOF

Cannot connect to: $DATABASE_URL

Create the role and database first (local dev only — see README for prod):

  sudo -u postgres psql -c "CREATE ROLE crm LOGIN PASSWORD 'crm' SUPERUSER;"
  sudo -u postgres psql -c "CREATE DATABASE crm OWNER crm;"

macOS/Homebrew: replace 'sudo -u postgres psql' with 'psql postgres'.
EOF
  exit 1
fi

say "Applying schema"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f db/schema.sql

# ------------------------------------------------------------------- seed
if [ -z "${OWNER_EMAIL:-}" ] || [ "${OWNER_EMAIL}" = "admin@example.com" ]; then
  say "Seeding demo workspace (owner: ${OWNER_EMAIL:-admin@example.com})"
  say "Set OWNER_EMAIL / OWNER_PASSWORD in .env to use your own."
fi
.venv/bin/python -m db.seed

# -------------------------------------------------------------------- run
cat <<EOF

  Sign in:   http://localhost:${PORT:-8000}/login
  Workspace: ${SEED_TENANT_SLUG:-demo}
  Email:     ${OWNER_EMAIL:-admin@example.com}
  Password:  (OWNER_PASSWORD from .env)

  Test form: http://localhost:${PORT:-8000}/form-example.html
  API docs:  http://localhost:${PORT:-8000}/api/docs

EOF
say "Starting server (Ctrl+C to stop)"
exec .venv/bin/uvicorn app.main:app --reload --port "${PORT:-8000}"
