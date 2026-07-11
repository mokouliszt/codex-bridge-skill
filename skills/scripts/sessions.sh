#!/bin/sh
# codex-bridge session manager
#
# Codexのセッション(会話履歴)は $CODEX_HOME/sessions/YYYY/MM/DD/
# rollout-<timestamp>-<session_id>.jsonl として永続化される。
# ただしClaudeサンドボックスは会話ごとにリセットされるため、
# 会話をまたいで継続したい場合は export/import を使う。
#
# usage:
#   sessions.sh list                 セッション一覧(id/日時/作業Dir/冒頭プロンプト)
#   sessions.sh export               全セッションを /mnt/user-data/outputs/ にtar.gz
#   sessions.sh import <tar.gz>      エクスポート済みアーカイブを復元
set -eu
export CODEX_HOME="${CODEX_HOME:-/home/claude/.codex-bridge}"

MODE="${1:-list}"

case "$MODE" in
  list)
    python3 - <<'PYEOF'
import glob, json, os, re

home = os.environ["CODEX_HOME"]
files = sorted(glob.glob(f"{home}/sessions/**/rollout-*.jsonl", recursive=True))
if not files:
    print("(セッションなし)")
rows = []
for f in files:
    sid, ts, cwd, prompt = "?", "?", "?", ""
    try:
        with open(f) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = d.get("payload", {})
                if d.get("type") == "session_meta":
                    sid = p.get("session_id") or p.get("id") or "?"
                    ts = (p.get("timestamp") or "?")[:19]
                    cwd = p.get("cwd", "?")
                cand = ""
                if p.get("type") == "user_message":
                    cand = str(p.get("message", ""))
                elif p.get("type") == "message" and p.get("role") == "user":
                    c = p.get("content", [])
                    if c and isinstance(c, list):
                        cand = str(c[0].get("text", ""))
                # 注入ブロック(<environment_context>等)はスキップし実プロンプトを探す
                if not prompt and cand and not cand.lstrip().startswith("<"):
                    prompt = cand[:60].replace("\n", " ")
                if prompt and sid != "?":
                    break
    except OSError:
        continue
    rows.append((ts, sid, cwd, prompt))
for ts, sid, cwd, prompt in sorted(rows):
    print(f"{ts}  {sid}\n    cwd={cwd}\n    prompt={prompt}")
PYEOF
    ;;
  export)
    [ -d "$CODEX_HOME/sessions" ] || { echo "セッションなし"; exit 1; }
    OUT="/mnt/user-data/outputs/codex-sessions-$(date +%Y%m%d-%H%M%S).tar.gz"
    tar -C "$CODEX_HOME" -czf "$OUT" sessions
    echo "exported: $OUT"
    echo "(ユーザーに保存してもらい、次の会話で import すると継続できる)"
    ;;
  import)
    [ $# -ge 2 ] || { echo "usage: sessions.sh import <tar.gz>"; exit 2; }
    mkdir -p "$CODEX_HOME"
    tar -C "$CODEX_HOME" -xzf "$2"
    echo "imported into $CODEX_HOME/sessions"
    sh "$0" list
    ;;
  *)
    echo "usage: sessions.sh list|export|import <tar.gz>"; exit 2 ;;
esac
