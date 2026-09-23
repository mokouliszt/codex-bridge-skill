#!/bin/sh
# codex-bridge setup (idempotent)
# - installs @openai/codex if missing
# - deploys bundled auth.json into an isolated CODEX_HOME
# - if there is no bundled auth.json AND none in CODEX_HOME yet (fresh/public
#   checkout with credentials stripped), automatically starts the phone-only
#   login flow (login.py gen) instead of just printing a hint -- the URL block
#   below is meant to be relayed to the user verbatim.
# - writes base config (web_search on; deliberately NO model/effort -- those are
#   chosen by the user and passed explicitly by ask.sh on every call)
# - leaves existing config intact
# - if auth/credentials.json is present, syncs auth.json with the S3-compatible
#   token store (token_store.py sync) so rotated refresh tokens survive the sandbox
set -eu
umask 077

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export CODEX_HOME="${CODEX_HOME:-/home/claude/.codex-bridge}"
mkdir -p "$CODEX_HOME" "$CODEX_HOME/bg"
chmod 700 "$CODEX_HOME" "$CODEX_HOME/bg" 2>/dev/null || true

# API key auth is forbidden by user policy: ChatGPT subscription only.
unset OPENAI_API_KEY CODEX_API_KEY 2>/dev/null || true

# 1) CLI
if ! command -v codex >/dev/null 2>&1; then
  echo "[setup] installing @openai/codex ..."
  npm install -g @openai/codex >/dev/null 2>&1
fi
echo "[setup] codex: $(codex --version)"

# 2) auth.json (ChatGPT subscription tokens)
#    Seed from the bundled copy, then let the optional token store
#    (auth/credentials.json, an S3-compatible bucket) replace it with the newest
#    rotated token and refresh it proactively if stale. Without
#    auth/credentials.json this step is skipped entirely (no boto3 install either).
if [ ! -f "$CODEX_HOME/auth.json" ] && [ -f "$SKILL_DIR/auth/auth.json" ]; then
  cp "$SKILL_DIR/auth/auth.json" "$CODEX_HOME/auth.json"
  chmod 600 "$CODEX_HOME/auth.json"
  echo "[setup] auth.json deployed to $CODEX_HOME"
fi
if [ -f "$SKILL_DIR/auth/credentials.json" ]; then
  if ! python3 -c "import boto3" >/dev/null 2>&1; then
    echo "[setup] installing boto3 for the token store ..."
    pip install -q boto3 --break-system-packages >/dev/null 2>&1 \
      || pip install -q boto3 >/dev/null 2>&1 \
      || echo "[setup] boto3 install failed -- token store disabled for this sandbox"
  fi
  python3 "$SKILL_DIR/scripts/token_store.py" sync || true
fi
if [ ! -f "$CODEX_HOME/auth.json" ]; then
  echo "[setup] NOT AUTHENTICATED: no auth/auth.json bundled with this skill, none in the token"
  echo "        store, and none in \$CODEX_HOME."
  echo "[setup] starting the phone-only login flow now (no PC required) ..."
  echo
  python3 "$SKILL_DIR/scripts/login.py" gen
  echo
  echo "[setup] ---"
  echo "[setup] relay the URL and instructions above to the user verbatim, then wait for them"
  echo "        to paste back the failed-redirect URL (the localhost:1455/... page). Once you"
  echo "        have it, run:"
  echo "        python3 \"$SKILL_DIR/scripts/login.py\" exchange \"<pasted URL>\""
  echo "        Do not call ask.sh again until auth.json exists -- it will refuse to run (exit 3)"
  echo "        rather than fail with a confusing raw CLI/HTTP error."
fi

# 2b) enforce "ChatGPT subscription only, never API-key billing" against the
#     credential CONTENTS, not just the env vars. Someone could hand-place an
#     API-key-style auth.json (OPENAI_API_KEY non-null) and the env-var unset
#     above wouldn't catch that -- the CLI would happily bill the API key.
if [ -f "$CODEX_HOME/auth.json" ]; then
  if python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)  # unreadable/malformed -- not our problem to diagnose here
sys.exit(1 if d.get("OPENAI_API_KEY") else 0)
' "$CODEX_HOME/auth.json"; then
    :
  else
    echo "[setup] POLICY VIOLATION: $CODEX_HOME/auth.json has a non-null OPENAI_API_KEY."
    echo "        This skill is ChatGPT-subscription-only by user policy. Refusing to"
    echo "        proceed with an API-key credential -- replace auth.json with a real"
    echo "        \`codex login\`-produced one (or use login.py's phone flow) before retrying."
    rm -f "$CODEX_HOME/auth.json"
    exit 4
  fi
fi

# 3) base config. No model / model_reasoning_effort on purpose: there is no
#    default -- ask.sh always passes the user's choice via -m / -c.
if [ ! -f "$CODEX_HOME/config.toml" ]; then
  cat > "$CODEX_HOME/config.toml" <<'EOF'
# codex-bridge base config (model/effort are chosen per call by the user)
check_updates = false
# rotated tokens must land in $CODEX_HOME/auth.json, where token_store.py reads them
cli_auth_credentials_store = "file"

[tools]
web_search = true
EOF
  echo "[setup] config.toml written"
fi

# 4) No automatic model selection or model_cache lookup. ask.sh refuses to run
#    without an explicit model and effort, so old caches/configs are never used.
#    Use models.sh choices to get the ranked list to offer the user.

# 5) status
codex login status || true
