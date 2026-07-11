#!/bin/sh
# codex-bridge setup (idempotent)
# - installs @openai/codex if missing
# - deploys bundled auth.json into an isolated CODEX_HOME
# - writes verified default config (gpt-5.6-sol / xhigh / web_search on)
# - resolves & caches the newest policy-compliant model (future-proofing)
set -eu

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export CODEX_HOME="${CODEX_HOME:-/home/claude/.codex-bridge}"
mkdir -p "$CODEX_HOME"

# API key auth is forbidden by user policy: ChatGPT subscription only.
unset OPENAI_API_KEY CODEX_API_KEY 2>/dev/null || true

# 1) CLI
if ! command -v codex >/dev/null 2>&1; then
  echo "[setup] installing @openai/codex ..."
  npm install -g @openai/codex >/dev/null 2>&1
fi
echo "[setup] codex: $(codex --version)"

# 2) auth.json (ChatGPT subscription tokens)
if [ ! -f "$CODEX_HOME/auth.json" ]; then
  if [ -f "$SKILL_DIR/auth/auth.json" ]; then
    cp "$SKILL_DIR/auth/auth.json" "$CODEX_HOME/auth.json"
    chmod 600 "$CODEX_HOME/auth.json"
    echo "[setup] auth.json deployed to $CODEX_HOME"
  else
    echo "[setup] WARNING: $SKILL_DIR/auth/auth.json not found."
    echo "         Re-auth: scripts/login.py (phone-only flow) or codex login on a PC."
  fi
fi

# 3) verified default config (codex-cli 0.144.1, 2026-07-10)
#    model/effort can still be overridden per-call by ask.sh
if [ ! -f "$CODEX_HOME/config.toml" ]; then
  cat > "$CODEX_HOME/config.toml" <<'EOF'
# codex-bridge defaults (verified 2026-07-10, codex-cli 0.144.1)
model = "gpt-5.6-sol"
model_reasoning_effort = "xhigh"
check_updates = false

[tools]
web_search = true
EOF
  echo "[setup] config.toml written"
fi

# 4) resolve newest policy-compliant model (>= xhigh support), cache it.
#    Best effort: on failure we keep the verified default.
if [ ! -f "$CODEX_HOME/model_cache" ]; then
  if RESOLVED=$(timeout 45 sh "$SKILL_DIR/scripts/models.sh" resolve 2>/dev/null) && [ -n "$RESOLVED" ]; then
    echo "$RESOLVED" > "$CODEX_HOME/model_cache"
    echo "[setup] newest policy-compliant model: $RESOLVED"
  else
    echo "[setup] model resolution skipped (using default gpt-5.6-sol)"
  fi
fi

# 5) status
codex login status || true
