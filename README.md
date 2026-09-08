# codex-bridge

A Claude.ai [Skill](https://www.anthropic.com/news/skills) that runs the official OpenAI Codex CLI **inside Claude's own code-execution sandbox**, authenticated with your ChatGPT subscription (Codex OAuth — no API key). It lets Claude consult GPT for a second opinion on hard problems, or delegate token-heavy work to it, all within one conversation.

*[日本語版はこちら (README.ja.md)](README.ja.md)*

> Unofficial, community project. Not affiliated with OpenAI or Anthropic. Depends on undocumented behavior (backend routing under ChatGPT auth) that could change without notice — see [Disclaimer](#disclaimer).

## What it does

- **Knowledge support (consult mode)** — Claude asks GPT-6.0 Astra (medium by default; low for simple work, web search on) for a second opinion on hard or specialized questions, then verifies and integrates the answer.
- **Work offload (delegate mode)** — Claude hands off token-heavy generation, exhaustive work, or large file processing to Codex. Codex is told to write results to files; Claude only reads back what it needs, which is most of how this keeps Claude's own token usage down.
- **Survives Claude's own turn ending** — Claude's sandbox is torn down the moment it finishes a reply with no more tool calls, which normally kills anything still running (including a long Codex job). `ask.sh` automatically falls back to a background job + `job_id` once a call runs long, and `wait.sh` lets Claude poll for completion cheaply (one short tool call and a one-line result per poll — never the growing log) without ending its turn. See [`SKILL.md`](skills/codex-bridge/SKILL.md) for the full mechanism.
- **Session management** — `--continue` / `--resume <id>` for follow-ups that keep prior context. `sessions.sh export`/`import` carries a session across separate Claude conversations (the sandbox itself resets between them).
- **Astra model policy** — defaults to `gpt-6-astra` + `medium`. Without an explicit user request, Claude uses only Astra `low` or `medium`, including retries and resumed sessions. Other models or higher effort require an explicit user request. No automatic newest-model adoption or fallback; legacy model caches are ignored.
- **Full Codex capability, unlocked** — web search on by default, least-restrictive execution flags (appropriate since it's already running inside Claude's own sandboxed environment), nested sandboxing (`codex sandbox`) for anything Codex should isolate further, `ultra` effort (automatic sub-agent task delegation) only when explicitly requested by the user.
- **Phone-only authentication** — no PC required. `scripts/login.py` walks through a PKCE-based OAuth flow entirely from the Claude mobile app: Claude prints an authorization URL, you complete it in your browser, then paste back the failed-redirect page's URL.
- **Self-healing auth** — if `auth.json` is missing entirely (e.g. a fresh checkout of this repo) or its refresh token has died, `setup.sh`/`ask.sh` detect it and automatically start the same phone-only re-auth flow instead of surfacing a raw CLI error.

## How it works

```
Claude.ai (web / mobile)
 └─ Claude's sandbox (code execution)
     ├─ npm install -g @openai/codex        ← setup.sh (once per sandbox)
     ├─ $CODEX_HOME/auth.json               ← your ChatGPT OAuth tokens
     └─ codex exec "prompt"                 ← ask.sh (foreground, or backgrounded + polled via wait.sh for long jobs)
         └─ chatgpt.com/backend-api/codex/… ← GPT-6.0 Astra answers, billed to your subscription
```

No API key (metered billing) is ever used — the scripts force-unset `OPENAI_API_KEY`/`CODEX_API_KEY`, and `setup.sh` now also rejects an `auth.json` whose contents are API-key-shaped, not just the env vars. Token refresh is handled automatically by the official CLI.

## Requirements

- A Claude.ai plan with Skills / code execution
- A ChatGPT plan with Codex access (Plus, Pro, etc.)
- A way to produce `auth.json` (either of):
  - A PC where you can run the Codex CLI once, **or**
  - Just your phone (the `login.py` flow below)

## Setup

### 1. Get an `auth.json`

**If you have a PC (fastest):**

```bash
npm install -g @openai/codex
codex login       # opens a browser; sign in with your ChatGPT account (Google sign-in works)
```

Copy the resulting `~/.codex/auth.json` (Windows: `%USERPROFILE%\.codex\auth.json`) into this skill's `auth/auth.json`.

**Phone only, no PC:**

Upload the skill without an `auth.json` and tell Claude you want to authenticate with `login.py`. Claude generates an authorization URL → you open it and sign in → you copy the address-bar URL of the resulting failed-redirect page (`localhost:1455/...`) back to Claude, which exchanges it for `auth.json`. See the re-auth walkthrough in [`references/codex-cli-reference.md`](skills/codex-bridge/references/codex-cli-reference.md). This same flow now also **starts automatically** — Claude will kick it off itself the moment it notices `auth.json` is missing or its refresh token has died, so you don't need to ask for it by name.

### 2. Zip it up and upload to Claude.ai

```bash
zip -r codex-bridge.skill.zip codex-bridge/
```

Upload from Claude.ai → Settings → Capabilities → Skills. Once uploaded, it's available from both the web and mobile apps on that account.

### 3. Use it

Just ask naturally in conversation:

- "**Ask Codex**: what's the current best practice for ○○?"
- "**Have Codex do** this refactor — write the result to `out/`"
- "**Continuing that Codex session** from before, have it write tests too"

Claude recognizes the skill and runs `setup.sh` → `ask.sh` on its own — including, for long-running work, keeping the sandbox alive and polling `wait.sh` until Codex finishes.

Default: `gpt-6-astra` / `medium`; simple work may use `low`. An explicit model request can be passed via `CODEX_MODEL` or the third argument; an explicit effort request via the second argument. Unspecified fields keep their defaults. Invalid effort values are rejected.

## Scripts

| Script | Role |
|---|---|
| `scripts/setup.sh` | Installs the CLI, deploys `auth.json`, writes Astra medium defaults (existing config is preserved; `ask.sh` supplies model/effort explicitly). Idempotent. Also validates `auth.json` isn't API-key-shaped and starts phone-only re-auth automatically when credentials are missing. |
| `scripts/ask.sh` | Main entry point: `"prompt" [low\|medium\|high\|xhigh\|max\|ultra] [model]`, `--continue`, `--resume <id>`. Waits inline up to `CODEX_INLINE_MAX_SECONDS` (default 240s), then automatically falls back to a background job + `job_id` (`CODEX_ASYNC=1` skips straight to background for jobs you already expect to run long). |
| `scripts/wait.sh` | Polls a backgrounded job (`<job_id> [max_wait_seconds] [poll_interval_seconds]`). Each call blocks internally via a real sleep loop — Claude just calls it again until it reports done, keeping its own token use to one short tool call per poll. Detects a crashed/orphaned job (process gone but no completion marker) rather than reporting "running" forever. |
| `scripts/jobs.sh` | `list` / `clean [max_age_seconds]` for background jobs — recovery if a `job_id` gets lost, and housekeeping for finished jobs. |
| `scripts/_lib.sh` | Shared shell functions (`ask.sh`/`wait.sh` only — not meant to be run directly): job-id validation, liveness/crash detection, and the auth-failure auto-recovery trigger. |
| `scripts/models.sh` | Lists models available on your plan (`list`) or checks that `gpt-6-astra` supports low/medium (`resolve`, no fallback). |
| `scripts/sessions.sh` | Lists / exports / imports Codex sessions, for continuing work across separate Claude conversations. |
| `scripts/login.py` | The phone-only PKCE auth flow — generates and exchanges `auth.json` without a PC. |

Operational knowledge for Claude itself (verified flags, config, the app-server RPC protocol, background-job internals, and a self-diagnosis procedure for future CLI changes) lives in [`references/codex-cli-reference.md`](skills/codex-bridge/references/codex-cli-reference.md).

## Security notes

- **`auth/auth.json` is a full-access token for your ChatGPT account.**
  - It is not included in this repository (`.gitignore`d). **Never commit it**, in a fork or otherwise.
  - Keep any skill zip that contains it inside your own Claude.ai account only.
- Using Codex consumes your subscription's usage allowance (rough measured cost: ~2.2k tokens for a simple answer, ~6.4k with web search, ~7.5k+ to resume a session, scaling with history length).
- `setup.sh` sets a restrictive `umask` and 700/600 permissions on everything under `$CODEX_HOME`, including background job directories, since job logs and prompts can contain the same kind of content as the rest of your conversation.

## Disclaimer

This is an unofficial tool, unaffiliated with OpenAI or Anthropic, and it depends on behavior (backend routing under ChatGPT auth) that isn't officially documented. Review the terms of service of both platforms yourself and use this at your own risk. It may stop working without notice if either side changes something — see `references/codex-cli-reference.md` for the self-diagnosis steps to work through if that happens.

## Verified environment

- codex-cli 0.144.1 (2026-07) and 0.150.1 (2026-08, execution/auth paths re-verified) on a Claude.ai sandbox (Ubuntu 24, Node 22, Python 3.12)
- Model: `gpt-5.6-sol` (`xhigh` / `max` / `ultra`), verified end-to-end including a real expired-token recovery and a real long-running (714s) delegated task that exercised the background/poll path

## License

[MIT](LICENSE)
