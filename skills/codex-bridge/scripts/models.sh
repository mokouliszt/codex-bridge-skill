#!/bin/sh
# codex-bridge model discovery via app-server JSON-RPC (model/list)
#
# usage:
#   models.sh list      human-readable table of models available to this account
#   models.sh resolve   print gpt-6-astra only if available with low and medium
#                       (never auto-select another model)
set -eu
export CODEX_HOME="${CODEX_HOME:-/home/claude/.codex-bridge}"
unset OPENAI_API_KEY CODEX_API_KEY 2>/dev/null || true

MODE="${1:-list}"

exec python3 - "$MODE" <<'PYEOF'
import subprocess, json, time, os, sys

mode = sys.argv[1]
p = subprocess.Popen(["codex", "app-server"],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True, env={**os.environ})

def send(o):
    p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()

send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
      "params": {"clientInfo": {"name": "codex-bridge", "title": "codex-bridge", "version": "1.0"}}})

data, t = None, time.time()
while time.time() - t < 30:
    line = p.stdout.readline()
    if not line:
        break
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    if msg.get("id") == 0:
        send({"jsonrpc": "2.0", "method": "initialized"})
        send({"jsonrpc": "2.0", "id": 1, "method": "model/list", "params": {}})
    if msg.get("id") == 1:
        data = msg.get("result", {}).get("data")
        break
p.kill()

if not data:
    sys.exit("model/list failed (protocol change? see references/codex-cli-reference.md)")

def efforts(m):
    return [e.get("reasoningEffort") for e in (m.get("supportedReasoningEfforts") or [])]

if mode == "list":
    for m in data:
        print(f"{m.get('id'):20s} default={str(bool(m.get('isDefault'))):5s} "
          f"efforts={'/'.join(efforts(m))}")
    sys.exit(0)

if mode != "resolve":
    sys.exit("usage: models.sh [list|resolve]")

# Pin the user-selected Astra family; do not follow isDefault or newer models.
for m in data:
    if (m.get("id") == "gpt-6-astra" and not m.get("hidden")
            and {"low", "medium"}.issubset(efforts(m))):
        print(m["id"])
        sys.exit(0)
sys.exit("gpt-6-astra with low/medium is unavailable; no automatic fallback. "
         "Run models.sh list and ask the user before choosing another model.")
PYEOF
