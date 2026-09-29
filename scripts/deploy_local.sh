#!/usr/bin/env bash
# One-command local setup, build and serve for Schwinger-ELO (spec §6).
#
#   ./scripts/deploy_local.sh [--sample] [--skip-crawl] [--refresh] [--test]
#                             [--no-serve] [--port N]
#
# Pipeline logic lives in `python -m src.cli`; this script only prepares the
# environment and calls the CLI. Safe to re-run; works from any directory.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/deploy_local.sh [options]

  --sample       Offline demo data, no network
  --skip-crawl   Rebuild from already cached/processed data only
  --refresh      Re-fetch pages even if cached
  --test         Run the test suite (pytest -q) before building
  --no-serve     Build only, don't start the server
  --port N       Server port (default 8000)
  -h, --help     Show this help

Environment: PYTHON=/path/to/python3.x selects the interpreter (>= 3.11).
EOF
}

step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- arguments
SAMPLE=0; SKIP_CRAWL=0; REFRESH=0; RUN_TESTS=0; SERVE=1; PORT=8000
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sample)     SAMPLE=1 ;;
    --skip-crawl) SKIP_CRAWL=1 ;;
    --refresh)    REFRESH=1 ;;
    --test)       RUN_TESTS=1 ;;
    --no-serve)   SERVE=0 ;;
    --port)
      [[ $# -ge 2 ]] || die "--port needs a value"
      PORT="$2"; shift ;;
    --port=*)     PORT="${1#--port=}" ;;
    -h|--help)    usage; exit 0 ;;
    *)            usage >&2; die "unknown option: $1" ;;
  esac
  shift
done
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) \
  || die "invalid port: $PORT"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
VENV="$REPO_ROOT/.venv"
VPY="$VENV/bin/python"
REQ="$REPO_ROOT/requirements.txt"
HASH_FILE="$VENV/.requirements.sha256"

# ---------------------------------------------------------------- 1. preflight
step "Preflight: looking for Python >= 3.11"
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }
PY=""
if [[ -n "${PYTHON:-}" ]]; then
  py_ok "$PYTHON" || die "PYTHON=$PYTHON is not a working Python >= 3.11"
  PY="$PYTHON"
else
  for cand in python3 python3.14 python3.13 python3.12 python3.11 python; do
    if command -v "$cand" >/dev/null 2>&1 && py_ok "$cand"; then PY="$(command -v "$cand")"; break; fi
  done
fi
[[ -n "$PY" ]] || die "Python >= 3.11 not found. Install it (e.g. 'sudo apt install python3.12 python3.12-venv' or from python.org) or set PYTHON=/path/to/python."
echo "Using $PY ($("$PY" --version 2>&1))"

# ---------------------------------------------------------------- 2. environment
step "Environment: .venv"
if [[ -x "$VPY" ]] && ! py_ok "$VPY"; then
  echo "Existing .venv is broken or too old - recreating."
  rm -rf "$VENV"
fi
if [[ ! -x "$VPY" ]]; then
  echo "Creating .venv"
  if ! "$PY" -m venv "$VENV" >/dev/null 2>&1; then
    # Debian/Ubuntu without python3.X-venv have no ensurepip: create the venv
    # without pip; pip is bootstrapped below via the official get-pip.py.
    echo "'venv' with pip failed (ensurepip missing?) - creating .venv without pip"
    rm -rf "$VENV"
    "$PY" -m venv --without-pip "$VENV" || die "could not create .venv"
  fi
fi
if ! "$VPY" -m pip --version >/dev/null 2>&1; then
  echo "pip missing in .venv - bootstrapping via https://bootstrap.pypa.io/get-pip.py"
  rm -f "$HASH_FILE"
  GETPIP="$VENV/get-pip.py"
  "$VPY" -c 'import sys, urllib.request; urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", sys.argv[1])' "$GETPIP" \
    || die "could not download get-pip.py (network?). Alternative: sudo apt install python3-venv, then delete .venv and re-run."
  "$VPY" "$GETPIP" --quiet || die "pip bootstrap via get-pip.py failed"
  rm -f "$GETPIP"
fi
REQ_HASH="$("$VPY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$REQ")"
if [[ -f "$HASH_FILE" && "$(cat "$HASH_FILE")" == "$REQ_HASH" ]]; then
  echo "requirements.txt unchanged - skipping install"
else
  echo "Installing requirements.txt"
  "$VPY" -m pip install --quiet --upgrade pip
  "$VPY" -m pip install --quiet -r "$REQ"
  echo "$REQ_HASH" > "$HASH_FILE"
fi

# ---------------------------------------------------------------- 3. tests
if (( RUN_TESTS )); then
  step "Tests: pytest -q"
  "$VPY" -m pytest -q
fi

# ---------------------------------------------------------------- 4. pipeline
CLI_ARGS=()
(( SAMPLE ))     && CLI_ARGS+=(--sample)
(( SKIP_CRAWL )) && CLI_ARGS+=(--skip-crawl)
(( REFRESH ))    && CLI_ARGS+=(--refresh)

if (( ! SAMPLE && ! SKIP_CRAWL )); then
  if [[ ! -d data/raw ]] || [[ -z "$(ls -A data/raw 2>/dev/null)" ]]; then
    echo
    echo "NOTE: first real-data crawl - this takes a long time (polite 0.5-1.0 s delay"
    echo "      per request). It is safe to interrupt: fetched pages are cached in"
    echo "      data/raw/ and the next run resumes. Use --sample for a quick demo."
  fi
fi

# ${arr[@]+...} form: empty arrays are safe under `set -u` on bash 3.2 (macOS).
step "Pipeline: python -m src.cli all ${CLI_ARGS[*]+${CLI_ARGS[*]}}"
"$VPY" -m src.cli all ${CLI_ARGS[@]+"${CLI_ARGS[@]}"}

# ---------------------------------------------------------------- 5. serve
if (( SERVE )); then
  step "Serve: http://localhost:$PORT/"
  exec "$VPY" -m src.cli serve --port "$PORT"
else
  step "Done: site built in $REPO_ROOT/dist (not serving; --no-serve)"
fi
