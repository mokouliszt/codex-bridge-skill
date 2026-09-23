#!/bin/sh
# codex-bridge model discovery via app-server JSON-RPC (model/list)
#
# usage:
#   models.sh list      human-readable table of models available to this account
#                       (raw order as returned by the CLI)
#   models.sh choices   same models, hidden ones dropped, ranked strongest-first
#                       for presenting a model choice to the user
#
# There is no default model: ask.sh requires an explicit model and effort, and
# Claude must ask the user when they have not specified them (see SKILL.md).
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

if mode != "choices":
    sys.exit("usage: models.sh [list|choices]")

# Heuristic capability ranking (strongest first), used only to ORDER the
# options shown to the user -- never to pick a model on the user's behalf.
#   1. capability tier from the id suffix: astra > sol > terra > luna
#      (a lower tier of a newer generation still ranks below a higher tier of
#      the previous one, e.g. gpt-5.6-sol above gpt-6-luna)
#   2. within a tier, newer generation first (gpt-6 > gpt-5.6)
#   3. ids with an unknown/absent tier suffix go last, newest generation first
# If OpenAI introduces a tier name not listed here, Claude should place it by
# judgment when building the choice list (see SKILL.md).
import re
TIER = {"astra": 0, "sol": 1, "terra": 2, "luna": 3}
UNKNOWN_TIER = len(TIER)

def rank_key(m):
    mid = m.get("id") or ""
    hit = re.match(r"^gpt-(\d+(?:\.\d+)*)(?:-([a-z]+))?", mid)
    if not hit:
        return (UNKNOWN_TIER + 1, (), mid)
    # pad so gpt-6.1 (6,1) sorts ahead of gpt-6 (6,0), not behind it
    parts = [int(x) for x in hit.group(1).split(".")][:4]
    gen = tuple(-x for x in parts + [0] * (4 - len(parts)))
    return (TIER.get(hit.group(2) or "", UNKNOWN_TIER), gen, mid)

ranked = sorted((m for m in data if not m.get("hidden")), key=rank_key)
for i, m in enumerate(ranked, 1):
    print(f"{i:2d}. {m.get('id'):20s} efforts={'/'.join(efforts(m))}")
PYEOF
