#!/bin/sh
# codex-bridge model discovery via app-server JSON-RPC (model/list)
#
# usage:
#   models.sh list      human-readable table of models available to this account
#   models.sh resolve   print the newest policy-compliant model id
#                       (policy: supports reasoning effort >= xhigh;
#                        pick highest gpt-<major>.<minor>; tie-break:
#                        isDefault > 'sol' variant > widest effort range)
set -eu
export CODEX_HOME="${CODEX_HOME:-/home/claude/.codex-bridge}"
unset OPENAI_API_KEY CODEX_API_KEY 2>/dev/null || true

MODE="${1:-list}"

exec python3 - "$MODE" <<'PYEOF'
import subprocess, json, time, os, re, sys

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

ORDER = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]

def efforts(m):
    return [e.get("reasoningEffort") for e in (m.get("supportedReasoningEfforts") or [])]

if mode == "list":
    for m in data:
        print(f"{m.get('id'):20s} default={str(bool(m.get('isDefault'))):5s} "
          f"efforts={'/'.join(efforts(m))}")
    sys.exit(0)

# resolve: policy filter -> xhigh or above supported
def version(mid):
    mt = re.match(r"gpt-(\d+)\.(\d+)", mid or "")
    return (int(mt.group(1)), int(mt.group(2))) if mt else (-1, -1)

cands = [m for m in data if "xhigh" in efforts(m) and not m.get("hidden")]
if not cands:
    sys.exit("no xhigh-capable model found")

best_ver = max(version(m["id"]) for m in cands)
cands = [m for m in cands if version(m["id"]) == best_ver]
cands.sort(key=lambda m: (bool(m.get("isDefault")),
                          "sol" in m["id"],
                          max(ORDER.index(e) for e in efforts(m) if e in ORDER)),
           reverse=True)
print(cands[0]["id"])
PYEOF
