# codex-bridge shared helpers -- sourced by ask.sh / wait.sh / jobs.sh, not run directly.
# Requires SKILL_DIR and CODEX_HOME to already be set by the caller.

# codex_bridge_valid_job_id <job_id>
# Rejects anything that isn't our own generated shape (date-pid[-hex]) before it
# ever touches a filesystem path. Blocks path traversal ("..", "/") if a job_id
# ever comes from somewhere less trustworthy than ask.sh's own output.
codex_bridge_valid_job_id() {
  case "$1" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]-*)
      case "$1" in
        */*|*..*) return 1 ;;
        *) return 0 ;;
      esac
      ;;
    *) return 1 ;;
  esac
}

# codex_bridge_job_alive <job_dir>
# True only if the pid in <job_dir>/pid both (a) answers to kill -0 and (b) has
# the same /proc start-time fingerprint recorded at launch. (b) is what keeps a
# reused PID from being misread as "our job is still running" -- kill -0 alone
# can't tell the difference between our process and an unrelated one that later
# # can't tell the difference between our process and an unrelated one that later
# reused the same number.
#
# IMPORTANT (found by actually SIGKILLing a job under test): once ask.sh's own
# process exits after backgrounding a job, the setsid'd runner is immediately
# reparented to PID 1. If this sandbox's PID 1 isn't a reaping init (common in
# minimal containers), a dead runner sits as a zombie indefinitely -- and
# `kill -0` reports SUCCESS for a zombie PID, since it still occupies a process
# table slot. A liveness check built on kill -0 alone therefore never fires:
# every crashed job looks eternally "running". The fix is to read the actual
# state char out of /proc/<pid>/stat instead and treat 'Z' as not-alive.
codex_bridge_job_alive() {
  _jd="$1"
  _pid=$(cat "$_jd/pid" 2>/dev/null) || return 1
  [ -n "$_pid" ] || return 1
  _stat=$(cat "/proc/$_pid/stat" 2>/dev/null) || return 1
  # comm (2nd field) is user-controlled and parenthesized, so split on the
  # LAST ") " rather than by position -- state is the first field after it.
  _rest="${_stat##*) }"
  _state=$(printf '%s' "$_rest" | cut -d' ' -f1)
  [ "$_state" = "Z" ] && return 1
  if [ -f "$_jd/pid_start" ]; then
    _rec=$(cat "$_jd/pid_start" 2>/dev/null)
    _cur=$(printf '%s' "$_rest" | cut -d' ' -f20)   # starttime: proc-stat field 22 overall
    [ -n "$_rec" ] && [ -n "$_cur" ] && [ "$_rec" != "$_cur" ] && return 1
  fi
  return 0
}

# codex_bridge_mark_orphan <job_dir>
# Synthesizes the same done/exit_code shape a normal finish would leave, so
# every downstream consumer (wait.sh, jobs.sh list/clean) keeps one code path
# instead of special-casing "the process just isn't there anymore" everywhere.
# exit_code 125 is our own sentinel (not one codex itself would produce via a
# clean exit) meaning "detected dead with no done marker -- crashed, OOM-killed,
# or a reused PID; not a real codex exit code".
codex_bridge_mark_orphan() {
  _jd="$1"
  { echo "[codex-bridge] job process is gone but no done/exit_code was written --"
    echo "treating as crashed (OOM-kill, SIGKILL, or a reused PID slipped past kill -0)."
  } >> "$_jd/log" 2>/dev/null
  echo 125 > "$_jd/exit_code"
  : > "$_jd/done"
}

# codex_bridge_report_failure <job_dir> <exit_code>
#
# A finished job's log is often 30-40 lines of rust ERROR noise even for a
# single root cause (each retried sub-call logs its own failure). Rather than
# dumping all of that into the conversation, this pattern-matches the one
# failure mode worth handling specially -- a dead/expired refresh token --
# and if that's what happened, immediately starts the same phone-only re-auth
# flow setup.sh uses for a totally-missing auth.json (login.py gen), so
# Claude gets an actionable URL in the SAME step it learns something is
# wrong, instead of needing a follow-up round trip to look up what a 401
# means. Any other failure just gets a short tail instead of the full log.
#
# The match is anchored to codex's own auth-manager log component (or a bare
# leading "ERROR:" summary line) rather than a bare substring search, so a
# job whose PROMPT or genuinely-generated ANSWER happens to discuss refresh
# tokens (e.g. asking codex to review this very file) can't false-trigger a
# re-auth just because the words appear somewhere in a 40-line log.
#
# Idempotent: only fires login.py gen once per job (a "reauth_started"
# sentinel is left in the job dir) so re-polling an already-reported job
# doesn't keep regenerating -- and thereby invalidating -- the PKCE state a
# user may already be part-way through using.
codex_bridge_report_failure() {
  _jd="$1"
  _exitcode="$2"
  _log="$_jd/log"
  if grep -qE "^([0-9TZ:.-]+[[:space:]]+)?ERROR.*(could not be refreshed|Invalid refresh token|invalid_refresh_token)" "$_log" 2>/dev/null; then
    # A dead token here often just means another conversation already rotated
    # it; if the token store holds a different (newer) one, adopt it and retry
    # instead of sending the user through a full re-login.
    _since=$(sed -n 's/^started_at=//p' "$_jd/meta" 2>/dev/null)
    if [ ! -f "$_jd/reauth_started" ] && python3 "$SKILL_DIR/scripts/token_store.py" recover ${_since:+--since "$_since"}; then
      echo "status=failed auth_status=recovered exit_code=$_exitcode"
      echo "[codex-bridge] the local refresh token was stale, but the token store had a newer one"
      echo "        and it is now in place -- re-run the same ask.sh call (no re-login needed)."
      return 0
    fi
    echo "status=failed auth_status=expired exit_code=$_exitcode"
    if [ -f "$_jd/reauth_started" ]; then
      echo "[codex-bridge] re-auth was already started for this job (see the URL from the first"
      echo "        report) -- not regenerating it; a second login.py gen would invalidate the"
      echo "        PKCE state if the user is mid-flow on the first one."
      return 0
    fi
    : > "$_jd/reauth_started"
    echo "[codex-bridge] auth.json exists but the refresh token is dead (expired/revoked)."
    echo "[codex-bridge] starting the phone-only re-auth flow now ..."
    echo
    python3 "$SKILL_DIR/scripts/login.py" gen
    echo
    echo "[codex-bridge] relay the URL above to the user verbatim, then wait for them to paste"
    echo "        back the failed-redirect URL, then run:"
    echo "        python3 \"$SKILL_DIR/scripts/login.py\" exchange \"<pasted URL>\""
    echo "        (this overwrites \$CODEX_HOME/auth.json directly -- no other cleanup needed)"
    echo "[codex-bridge] note: codex login --device-auth also exists on this CLI (0.150.1+) and"
    echo "        avoids this whole copy-paste dance, but needs device-code sign-in enabled in"
    echo "        the user's ChatGPT security settings first and hasn't been exercised by this"
    echo "        skill yet -- ask the user if they'd rather try that instead."
  else
    echo "status=failed auth_status=unknown exit_code=$_exitcode"
    echo "--- log tail ---"
    tail -n 15 "$_log" 2>/dev/null || true
  fi
}
