#!/bin/sh
# codex-bridge background job list / cleanup
# usage:
#   jobs.sh list                    id/状態/開始時刻/プロンプト冒頭を一覧
#   jobs.sh clean [max_age_seconds] 完了済み(done)ジョブのうち指定秒数より古いものを削除
#                                    (既定86400=24h。running中のジョブは対象外)
set -eu
export CODEX_HOME="${CODEX_HOME:-/home/claude/.codex-bridge}"
BG="$CODEX_HOME/bg"
MODE="${1:-list}"

[ -d "$BG" ] || { echo "(バックグラウンドジョブなし)"; exit 0; }

case "$MODE" in
  list)
    FOUND=0
    for d in "$BG"/*/; do
      [ -d "$d" ] || continue
      FOUND=1
      jid=$(basename "$d")
      if [ -f "$d/done" ]; then
        ec=$(cat "$d/exit_code" 2>/dev/null || echo "?")
        if [ "$ec" = "125" ]; then
          st="crashed(orphan/OOM/SIGKILL detected)"
        else
          st="done(exit=$ec)"
        fi
      else
        st="running"
      fi
      prompt=$(sed -n 's/^prompt_head=//p' "$d/meta" 2>/dev/null || echo "?")
      started=$(sed -n 's/^started_at=//p' "$d/meta" 2>/dev/null || echo "?")
      echo "$jid  status=$st  started_epoch=$started  prompt=$prompt"
    done
    [ "$FOUND" = "0" ] && echo "(バックグラウンドジョブなし)"
    ;;
  clean)
    MAXAGE="${2:-86400}"
    NOW=$(date +%s)
    REMOVED=0
    for d in "$BG"/*/; do
      [ -d "$d" ] || continue
      [ -f "$d/done" ] || continue
      started=$(sed -n 's/^started_at=//p' "$d/meta" 2>/dev/null || echo "$NOW")
      age=$((NOW - started))
      if [ "$age" -ge "$MAXAGE" ]; then
        echo "removed: $(basename "$d") (age=${age}s)"
        rm -rf "$d"
        REMOVED=$((REMOVED + 1))
      fi
    done
    [ "$REMOVED" = "0" ] && echo "(削除対象なし)"
    ;;
  *)
    echo "usage: jobs.sh list|clean [max_age_seconds]"; exit 2 ;;
esac
exit 0
