#!/usr/bin/env python3
"""codex-bridge token store: keep the rotating ChatGPT refresh token alive
across Claude conversations.

Why this exists
---------------
ChatGPT OAuth refresh tokens are single-use: every refresh returns a NEW
refresh token and invalidates the old one. The Codex CLI refreshes on its own,
but it can only write the result to $CODEX_HOME/auth.json inside this sandbox,
which is thrown away when the conversation ends, while the skill's bundled
auth/auth.json is read-only. So without somewhere writable that outlives the
sandbox, the first refresh after bundling "uses up" the bundled token and the
next conversation needs a full re-login.

This script persists auth.json as one object in an S3-compatible bucket
(Backblaze B2, Cloudflare R2, AWS S3, MinIO, ...), so every conversation starts
from the newest token instead of the bundled one.

It is entirely optional: with no auth/credentials.json, every subcommand is a
no-op (exit 0; `recover` exits 1) and the skill behaves exactly as before.

auth/credentials.json (never committed; see credentials.json.example). :
  endpoint          S3 endpoint host (or full URL), e.g. s3.us-east-005.backblazeb2.com
  region            e.g. us-east-005
  bucket            bucket name
  key_id            access key id      (B2: keyID)
  application_key   secret access key  (B2: applicationKey)
  object_key        optional, default "_codex-bridge/auth.json"
Requires boto3 (setup.sh installs it only when credentials.json exists).

usage
  token_store.py sync      setup-time: adopt whichever of local/remote is newer,
                           refresh proactively if stale, and push the result.
                           exit 10 (EXIT_RELOGIN) = the refresh was rejected and
                           the store holds nothing newer: the token is dead and
                           setup.sh should start the login flow right away
  token_store.py push      after a codex run: upload local auth.json if the CLI
                           rotated it (no-op when unchanged)
  token_store.py recover [--since <epoch>]
                           after a dead-refresh-token failure: adopt the remote
                           copy if it holds a different, not-older token
                           (exit 0 = retry).
                           --since = failed job's start time (see cmd_recover)
  token_store.py status    one-line summary (dates only, never token values)

A stored object that is not a usable ChatGPT auth.json (bad JSON, API-key auth,
no refresh token) is treated as absent by sync/push, so a valid local token
overwrites it. Nothing here ever prints token values.
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODEX_HOME = os.environ.get("CODEX_HOME", "/home/claude/.codex-bridge")
LOCAL = os.path.join(CODEX_HOME, "auth.json")
CRED_FILE = os.environ.get("CODEX_BRIDGE_STORE_CREDENTIALS",
                           os.path.join(SKILL_DIR, "auth", "credentials.json"))
DEFAULT_OBJECT_KEY = "_codex-bridge/auth.json"

# Same client/endpoint/scope the Codex CLI itself uses for refresh (verified
# against the codex-cli 0.156.x binary; the CLI also honours this override env).
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_URL = os.environ.get("CODEX_REFRESH_TOKEN_URL_OVERRIDE", "https://auth.openai.com/oauth/token")
REFRESH_SCOPE = "openid profile email"

# The CLI refreshes once last_refresh is ~8 days old; refresh a day earlier so
# the rotation happens here (and is pushed immediately) instead of inside a
# codex run whose sandbox might be torn down before we can push.
STALE_DAYS = float(os.environ.get("CODEX_BRIDGE_REFRESH_DAYS", "7"))
AT_MARGIN_S = 2 * 86400  # also refresh if the access token expires within 2 days

# `sync` exit code meaning "the local token is dead and the store has nothing
# newer -- start the login flow now" (setup.sh acts on it). Distinct from 1/2
# (generic failure / usage) so a stray error is never mistaken for it.
EXIT_RELOGIN = 10


def log(msg):
    print(f"[token-store] {msg}")


# ---------------------------------------------------------------- config

class StoreError(RuntimeError):
    pass


class CorruptObject(StoreError):
    """The stored object exists but is not a usable ChatGPT auth.json."""


def load_config():
    """-> dict, or None when the token store is not configured (feature off)."""
    if not os.path.isfile(CRED_FILE):
        return None
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise StoreError(f"cannot read {os.path.basename(CRED_FILE)}: {e.__class__.__name__}")
    missing = [k for k in ("endpoint", "bucket", "key_id", "application_key") if not cfg.get(k)]
    if missing:
        raise StoreError(f"{os.path.basename(CRED_FILE)} is missing: {', '.join(missing)}")
    ep = cfg["endpoint"].strip().rstrip("/")
    cfg["endpoint_url"] = ep if re.match(r"https?://", ep) else "https://" + ep
    cfg["object_key"] = (cfg.get("object_key") or DEFAULT_OBJECT_KEY).lstrip("/")
    return cfg


# ---------------------------------------------------------------- token helpers

def jwt_claims(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
    except Exception:  # noqa: BLE001
        return {}
    return claims if isinstance(claims, dict) else {}


def parse_ts(s):
    if not s or not isinstance(s, str):
        return 0.0
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?", s)
    if not m:
        return 0.0
    base = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return base.timestamp() + float(m.group(2) or 0)


def _tokens(auth):
    t = auth.get("tokens") if isinstance(auth, dict) else None
    return t if isinstance(t, dict) else {}


def freshness(auth):
    """When this token set was issued (access-token iat, else last_refresh)."""
    if not isinstance(auth, dict):
        return 0.0
    iat = jwt_claims(_tokens(auth).get("access_token", "")).get("iat")
    try:
        return float(iat) if iat else parse_ts(auth.get("last_refresh"))
    except (TypeError, ValueError):
        return parse_ts(auth.get("last_refresh"))


def rt_hash(auth):
    rt = _tokens(auth).get("refresh_token")
    return hashlib.sha256(rt.encode()).hexdigest() if isinstance(rt, str) and rt else ""


def fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts else "-"


def is_stale(auth):
    now = time.time()
    last = parse_ts(auth.get("last_refresh")) or freshness(auth)
    if now - last >= STALE_DAYS * 86400:
        return True
    exp = jwt_claims(_tokens(auth).get("access_token", "")).get("exp")
    try:
        return bool(exp) and float(exp) - now < AT_MARGIN_S
    except (TypeError, ValueError):
        return True


def valid_chatgpt_auth(auth):
    return (isinstance(auth, dict) and not auth.get("OPENAI_API_KEY")
            and bool(rt_hash(auth)))


def read_local():
    try:
        with open(LOCAL, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_local(auth):
    os.makedirs(CODEX_HOME, exist_ok=True)
    tmp = LOCAL + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(auth, f, indent=2)
    os.replace(tmp, LOCAL)


# ---------------------------------------------------------------- S3 store

_s3 = None


def s3(cfg):
    global _s3
    if _s3 is None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise StoreError("boto3 is not installed (pip install boto3 --break-system-packages)")
        _s3 = boto3.client(
            "s3", endpoint_url=cfg["endpoint_url"], region_name=cfg.get("region") or None,
            aws_access_key_id=cfg["key_id"], aws_secret_access_key=cfg["application_key"],
            # "when_required": the newer boto3 default checksum trailers are not
            # supported by every S3-compatible provider
            config=Config(signature_version="s3v4", retries={"max_attempts": 3},
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required",
                          connect_timeout=10, read_timeout=20,
                          s3={"addressing_style": cfg.get("addressing_style", "path")}))
    return _s3


def _client_error(e, op):
    err = getattr(e, "response", {}).get("Error", {})
    return StoreError(f"S3 {op} failed ({err.get('Code', e.__class__.__name__)})"
                      " -- check endpoint/bucket/key permissions in credentials.json")


def s3_pull(cfg):
    """-> auth dict, or None if the object does not exist yet.
    Raises CorruptObject if it exists but is not a usable ChatGPT auth.json."""
    client = s3(cfg)  # first: turns a missing boto3 into StoreError
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        obj = client.get_object(Bucket=cfg["bucket"], Key=cfg["object_key"])
        body = obj["Body"].read()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        raise _client_error(e, "get")
    except BotoCoreError as e:
        raise StoreError(f"S3 get failed ({e.__class__.__name__})")
    try:
        auth = json.loads(body)
    except ValueError:
        raise CorruptObject("stored object is not valid JSON")
    if not valid_chatgpt_auth(auth):
        raise CorruptObject("stored object is not a ChatGPT auth.json with a refresh token")
    return auth


def pull_or_none(cfg, note=True):
    """s3_pull, but a corrupt object reads as absent so callers holding a valid
    local token overwrite it (self-heal) instead of warning forever."""
    try:
        return s3_pull(cfg)
    except CorruptObject as e:
        if note:
            log(f"{e} -- treating it as empty; it will be replaced by a valid local token")
        return None


def s3_put(cfg, auth):
    client = s3(cfg)  # first: turns a missing boto3 into StoreError
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        client.put_object(Bucket=cfg["bucket"], Key=cfg["object_key"],
                           Body=json.dumps(auth, indent=2).encode(),
                           ContentType="application/json")
    except ClientError as e:
        raise _client_error(e, "put")
    except BotoCoreError as e:
        raise StoreError(f"S3 put failed ({e.__class__.__name__})")


# ---------------------------------------------------------------- OAuth refresh

class RefreshRejected(Exception):
    pass


def oauth_refresh(auth):
    tokens = auth["tokens"]
    body = json.dumps({"client_id": CLIENT_ID, "grant_type": "refresh_token",
                       "refresh_token": tokens["refresh_token"], "scope": REFRESH_SCOPE}).encode()
    req = urllib.request.Request(REFRESH_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        # 400/401 = refresh token reused/expired/revoked. Only the error code is
        # reported; the body can echo request details and is never printed.
        try:
            err = json.load(e).get("error", {})
            err = err.get("code") if isinstance(err, dict) else err
        except Exception:  # noqa: BLE001
            err = ""
        raise RefreshRejected(f"HTTP {e.code} {err or ''}".strip())
    new = json.loads(json.dumps(auth))
    for k in ("id_token", "access_token", "refresh_token"):
        if tok.get(k):
            new["tokens"][k] = tok[k]
    claims = jwt_claims(new["tokens"].get("id_token", ""))
    acct = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
    if acct:
        new["tokens"]["account_id"] = acct
    new["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return new


# ---------------------------------------------------------------- commands

def cmd_status(cfg):
    local = read_local()
    if not cfg:
        log(f"store=disabled local_issued={fmt(freshness(local))}")
        return 0
    try:
        remote = s3_pull(cfg)
        remote_issued = fmt(freshness(remote))
    except CorruptObject:
        remote, remote_issued = None, "corrupt"
    same = rt_hash(local) == rt_hash(remote) and bool(local)
    log(f"store=s3 local_issued={fmt(freshness(local))} remote_issued={remote_issued} "
        f"in_sync={'yes' if same else 'no'} local_stale={'yes' if local and is_stale(local) else 'no'}")
    return 0


def push_with_retry(cfg, auth):
    """Upload auth and verify it stuck. S3-compatible stores have no portable
    compare-and-swap, so a concurrent writer is detected by reading back: if a
    different, newer token landed, adopt it instead. -> the auth now in force."""
    for _ in range(3):
        s3_put(cfg, auth)
        time.sleep(float(os.environ.get("CODEX_BRIDGE_STORE_SETTLE", "1")))
        remote = pull_or_none(cfg, note=False)
        if rt_hash(remote) == rt_hash(auth):
            return auth
        if remote and valid_chatgpt_auth(remote) and freshness(remote) >= freshness(auth):
            write_local(remote)
            log("store was updated concurrently with a newer token -- adopted it")
            return remote
    raise StoreError("could not confirm the upload after 3 attempts")


def cmd_push(cfg):
    if not cfg:
        return 0
    local = read_local()
    if not valid_chatgpt_auth(local):
        return 0
    remote = pull_or_none(cfg)
    if rt_hash(local) == rt_hash(remote):
        return 0  # unchanged: the common case, stay silent
    if remote and freshness(remote) > freshness(local):
        write_local(remote)  # another conversation rotated it after we started
        log("remote copy is newer -- adopted it instead of pushing")
        return 0
    in_force = push_with_retry(cfg, local)
    if rt_hash(in_force) == rt_hash(local):
        # (when a concurrent newer token was adopted instead, push_with_retry
        #  has already said so -- claiming a push here would be wrong)
        log(f"pushed rotated token (issued {fmt(freshness(local))})")
    return 0


def cmd_sync(cfg):
    if not cfg:
        return 0
    local = read_local()
    remote = pull_or_none(cfg)
    if valid_chatgpt_auth(remote) and freshness(remote) > freshness(local):
        write_local(remote)
        local = remote
        log(f"adopted stored token (issued {fmt(freshness(local))})")
    if not valid_chatgpt_auth(local):
        log("no usable token locally or in the store -- login flow required")
        return 0

    if is_stale(local):
        tried = rt_hash(local)
        try:
            local = oauth_refresh(local)
            write_local(local)
            log(f"refreshed proactively (issued {fmt(freshness(local))})")
        except RefreshRejected as e:
            # Most likely another conversation refreshed with the same token a
            # moment ago; give its push a few seconds to land, then adopt it.
            for _ in range(3):
                time.sleep(2)
                remote = pull_or_none(cfg, note=False)
                if remote and rt_hash(remote) != tried and valid_chatgpt_auth(remote):
                    write_local(remote)
                    log("refresh was rejected but the store has a newer token -- adopted it")
                    return 0
            log(f"refresh rejected ({e}); the stored token is dead too -- login flow required")
            return EXIT_RELOGIN
        except (urllib.error.URLError, OSError) as e:
            log(f"refresh skipped (network: {e.__class__.__name__}); the CLI will retry on its own")

    if rt_hash(local) != rt_hash(remote):
        in_force = push_with_retry(cfg, local)
        if rt_hash(in_force) == rt_hash(local):
            log(f"store updated (issued {fmt(freshness(local))})")
    return 0


def cmd_recover(cfg, since=None):
    """Exit 0 if a retry would use a different token than the one that failed.

    `since` (epoch) is when the failed job started: if the job's own post-run
    push already adopted the newer stored token (auth.json rewritten after the
    job began), there is nothing left to adopt but a retry will still work.
    """
    if not cfg:
        return 1
    local = read_local()
    remote = s3_pull(cfg)  # corrupt -> StoreError -> warning + exit 1 in main()
    if not valid_chatgpt_auth(remote):
        return 1
    if rt_hash(remote) != rt_hash(local):
        if valid_chatgpt_auth(local) and freshness(remote) < freshness(local):
            # An older token than the one that just failed is almost surely
            # spent too; skip the wasted retry and go straight to re-login.
            log(f"stored token is older (issued {fmt(freshness(remote))}) than the failed one -- not adopting")
            return 1
        write_local(remote)
        log(f"adopted stored token (issued {fmt(freshness(remote))}) -- retry the same call")
        return 0
    try:
        replaced_after_start = since is not None and os.path.getmtime(LOCAL) > since
    except OSError:
        replaced_after_start = False
    if replaced_after_start:
        log("a newer stored token was already adopted after the failed run -- retry the same call")
        return 0
    return 1


def main():
    cmds = {"sync": cmd_sync, "push": cmd_push, "recover": cmd_recover, "status": cmd_status}
    args = sys.argv[1:]
    since = None
    if len(args) == 3 and args[0] == "recover" and args[1] == "--since" and args[2].isdigit():
        since, args = float(args[2]), args[:1]
    if len(args) != 1 or args[0] not in cmds:
        print(__doc__)
        return 2
    try:
        cfg = load_config()
        if args[0] == "recover":
            return cmd_recover(cfg, since)
        return cmds[args[0]](cfg)
    except ImportError as e:  # belt and braces: any missing boto3/botocore piece
        log(f"warning: boto3 is not usable ({e.__class__.__name__}: {e.name or e}) "
            "-- pip install boto3 --break-system-packages")
        return 1 if args[0] == "recover" else 0
    except (StoreError, urllib.error.URLError, OSError) as e:
        # Never fatal for the caller: the token store is a best-effort layer.
        log(f"warning: {e}")
        return 1 if args[0] == "recover" else 0


if __name__ == "__main__":
    sys.exit(main())
