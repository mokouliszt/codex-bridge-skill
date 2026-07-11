#!/usr/bin/env python3
"""codex-bridge in-sandbox PKCE login helper.

PCなし・スマホのClaudeアプリだけで auth.json を作るためのツール。
公式Codex CLIと同一のOAuthクライアント/パラメータを使う
(client_id等はCLIバイナリから確認済み: 2026-07, codex-cli 0.144.x)。

usage:
  python3 login.py gen
      -> 認可URLを表示。ユーザーはスマホのブラウザで開いてGoogleログイン。
         最後に http://localhost:1455/auth/callback?code=...&state=... へ
         リダイレクトされて「接続できません」になるので、その失敗した
         ページの**アドレスバーのURL全体をコピー**してClaudeに貼る。
         (PKCE verifierは /home/claude/.codex-bridge/pkce_state.json に保存)

  python3 login.py exchange "<貼られたURL または codeの値>"
      -> トークン交換して $CODEX_HOME/auth.json を生成。
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"

CODEX_HOME = os.environ.get("CODEX_HOME", "/home/claude/.codex-bridge")
STATE_FILE = os.path.join(CODEX_HOME, "pkce_state.json")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def cmd_gen() -> None:
    os.makedirs(CODEX_HOME, exist_ok=True)
    verifier = b64url(secrets.token_bytes(64))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = b64url(secrets.token_bytes(24))
    with open(STATE_FILE, "w") as f:
        json.dump({"verifier": verifier, "state": state}, f)
    os.chmod(STATE_FILE, 0o600)

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "codex_cli_rs",
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print("== この URL をユーザーのブラウザで開いてもらう ==")
    print(url)
    print()
    print("ログイン完了後、localhost:1455 への接続失敗ページになるので、")
    print("そのページのアドレスバーのURL全体(code=...を含む)をコピーして")
    print("チャットに貼ってもらうこと。")
    print(f"(container再起動に備えたverifier控え: {verifier})")


def cmd_exchange(arg: str, verifier_override) -> None:
    # arg は貼られたURL全体でも code 単体でもよい
    code = arg
    state_in = None
    if "code=" in arg:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(arg).query)
        code = q.get("code", [""])[0]
        state_in = q.get("state", [None])[0]
    if not code:
        sys.exit("ERROR: authorization code を抽出できません")

    verifier = verifier_override
    if not verifier:
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            verifier = saved["verifier"]
            if state_in and saved.get("state") and state_in != saved["state"]:
                print("WARNING: state不一致(別セッションのgen?)。続行はするが失敗したらgenからやり直し。")
        except FileNotFoundError:
            sys.exit("ERROR: pkce_state.json がない(コンテナがリセットされた)。"
                     "gen時に表示されたverifierを --verifier で渡すこと。")

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: token exchange failed {e.code}: {e.read().decode()[:500]}")

    id_token = tok.get("id_token", "")
    account_id = ""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        auth_claim = claims.get("https://api.openai.com/auth", {})
        account_id = auth_claim.get("chatgpt_account_id", "") or ""
        if not account_id:
            print("WARNING: chatgpt_account_id が見つからない。claims keys:",
                  list(claims.keys()))
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: id_token decode失敗: {e}")

    auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token,
            "access_token": tok.get("access_token", ""),
            "refresh_token": tok.get("refresh_token", ""),
            "account_id": account_id,
        },
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    os.makedirs(CODEX_HOME, exist_ok=True)
    out = os.path.join(CODEX_HOME, "auth.json")
    with open(out, "w") as f:
        json.dump(auth, f, indent=2)
    os.chmod(out, 0o600)
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass
    print(f"OK: {out} を生成 (refresh_token: {'あり' if auth['tokens']['refresh_token'] else 'なし!'})")
    print("次: `codex login status` で確認。skill zipのauth/auth.jsonも更新を忘れずに。")


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "gen":
        cmd_gen()
    elif len(sys.argv) >= 3 and sys.argv[1] == "exchange":
        verifier = None
        args = sys.argv[2:]
        if "--verifier" in args:
            i = args.index("--verifier")
            verifier = args[i + 1]
            del args[i:i + 2]
        cmd_exchange(args[0], verifier)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
