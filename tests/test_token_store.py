"""Offline token-store checks: python3 -m unittest discover -s tests -v.

A local HTTP server plays both an S3-compatible endpoint (path-style, as
boto3 talks to it) and the OAuth token endpoint (CODEX_REFRESH_TOKEN_URL_OVERRIDE),
so no real credentials or network access are involved. Requires boto3.
"""
import base64
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/codex-bridge/scripts/token_store.py'


def jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')
    return enc({'alg': 'none'}) + '.' + enc(claims) + '.sig'


def auth(rt, issued, api_key=None):
    return {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': api_key,
            'tokens': {'id_token': jwt({'https://api.openai.com/auth': {'chatgpt_account_id': 'acct'}}),
                       'access_token': jwt({'iat': issued, 'exp': issued + 10 * 86400}),
                       'refresh_token': rt, 'account_id': 'acct'},
            'last_refresh': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(issued))}


class Fake:
    """State shared with the handler: one stored object + a single-use RT chain."""
    def __init__(self):
        self.file = None          # dict or None
        self.valid_rt = None      # the only refresh token the fake OAuth accepts
        self.refreshes = 0
        self.puts = 0
        self.after_put = None     # hook: simulate a concurrent writer


KEY_PATH = '/tokens/_codex-bridge/auth.json'


class Handler(BaseHTTPRequestHandler):
    fake = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('ETag', '"etag"')
        self.end_headers()
        self.wfile.write(data)

    def _s3_error(self, code, s3code):
        self._send(code, ('<?xml version="1.0"?><Error><Code>%s</Code><Message>x</Message></Error>'
                          % s3code).encode(), 'application/xml')

    def _raw(self):
        return self.rfile.read(int(self.headers.get('Content-Length', 0)))

    def do_GET(self):
        f = self.fake
        assert self.headers['Authorization'].startswith('AWS4-HMAC-SHA256 Credential=test-key-id/')
        if urllib.parse.urlsplit(self.path).path != KEY_PATH:
            return self._s3_error(404, 'NoSuchBucket')
        if f.file is None:
            return self._s3_error(404, 'NoSuchKey')
        self._send(200, f.file)

    def do_PUT(self):
        f = self.fake
        assert urllib.parse.urlsplit(self.path).path == KEY_PATH
        assert 'aws-chunked' not in (self.headers.get('Content-Encoding') or '')
        f.file = json.loads(self._raw())
        f.puts += 1
        self._send(200, b'')
        if f.after_put:
            hook, f.after_put = f.after_put, None
            hook()

    def do_POST(self):  # OAuth refresh
        f = self.fake
        body = json.loads(self._raw() or b'{}')
        assert body['grant_type'] == 'refresh_token'
        if body['refresh_token'] != f.valid_rt:
            return self._send(401, {'error': {'code': 'refresh_token_reused', 'message': 'x'}})
        f.refreshes += 1
        new_rt = 'rt-%d' % (f.refreshes + 100)
        f.valid_rt = new_rt
        now = int(time.time())
        self._send(200, {'access_token': jwt({'iat': now, 'exp': now + 10 * 86400}),
                         'refresh_token': new_rt,
                         'id_token': jwt({'https://api.openai.com/auth': {'chatgpt_account_id': 'acct'}})})


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        Handler.fake = self.fake
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        base = 'http://127.0.0.1:%d' % self.srv.server_address[1]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / 'home'
        self.home.mkdir()
        self.envfile = Path(tmp.name) / 'credentials.json'
        self.write_creds()
        self.env = dict(os.environ, CODEX_HOME=str(self.home), CODEX_BRIDGE_STORE_CREDENTIALS=str(self.envfile),
                        CODEX_BRIDGE_STORE_SETTLE='0', CODEX_REFRESH_TOKEN_URL_OVERRIDE=base + '/oauth/token')
        self.base = base

    def write_creds(self, **over):
        port = self.srv.server_address[1]
        creds = {'provider': 'test', 'endpoint': 'http://127.0.0.1:%d' % port, 'region': 'us-test-1',
                 'bucket': 'tokens', 'key_id': 'test-key-id', 'application_key': 'test-secret'}
        creds.update(over)
        self.envfile.write_text(json.dumps(creds))

    def run_ts(self, *args):
        r = subprocess.run(['python3', str(SCRIPT), *args], env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertNotIn('rt-', r.stdout + r.stderr, 'token values must never be printed')
        return r

    def local(self):
        return json.loads((self.home / 'auth.json').read_text())

    def set_local(self, a):
        (self.home / 'auth.json').write_text(json.dumps(a))

    def test_disabled_without_env_file_is_noop(self):
        self.envfile.unlink()
        self.set_local(auth('rt-a', int(time.time()) - 30 * 86400))
        for cmd in ('sync', 'push'):
            r = self.run_ts(cmd)
            self.assertEqual((r.returncode, r.stdout), (0, ''))
        self.assertEqual(self.run_ts('recover').returncode, 1)
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-a')
        self.assertEqual(self.fake.puts + self.fake.refreshes, 0)

    def test_sync_seeds_empty_store_with_fresh_local(self):
        self.set_local(auth('rt-a', int(time.time()) - 86400))
        self.run_ts('sync')
        self.assertEqual(self.fake.file['tokens']['refresh_token'], 'rt-a')
        self.assertEqual(self.fake.refreshes, 0)

    def test_sync_prefers_newer_store_over_stale_bundle(self):
        now = int(time.time())
        self.set_local(auth('rt-bundled', now - 9 * 86400))   # dead bundled copy
        self.fake.file = auth('rt-stored', now - 86400)
        self.run_ts('sync')
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-stored')
        self.assertEqual((self.fake.refreshes, self.fake.puts), (0, 0))

    def test_sync_adopts_store_when_no_local_file(self):
        self.fake.file = auth('rt-stored', int(time.time()) - 86400)
        self.run_ts('sync')
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-stored')
        self.assertEqual(os.stat(self.home / 'auth.json').st_mode & 0o777, 0o600)

    def test_sync_refreshes_stale_token_and_pushes_rotation(self):
        now = int(time.time())
        self.fake.file = auth('rt-old', now - 8 * 86400)
        self.fake.valid_rt = 'rt-old'
        self.run_ts('sync')
        self.assertEqual(self.fake.refreshes, 1)
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-101')
        self.assertEqual(self.fake.file['tokens']['refresh_token'], 'rt-101')
        self.assertIsNone(self.local()['OPENAI_API_KEY'])

    def test_sync_rejected_refresh_adopts_concurrent_winner(self):
        now = int(time.time())
        self.set_local(auth('rt-old', now - 8 * 86400))
        self.fake.file = auth('rt-old', now - 8 * 86400)
        self.fake.valid_rt = 'rt-winner'   # someone else already rotated rt-old ...

        def winner_pushes_late():         # ... and their push lands a moment later
            time.sleep(1)
            self.fake.file = auth('rt-winner', int(time.time()))
        threading.Thread(target=winner_pushes_late).start()
        self.run_ts('sync')
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-winner')

    def test_push_uploads_cli_rotation_and_is_silent_when_unchanged(self):
        now = int(time.time())
        self.fake.file = auth('rt-a', now - 86400)
        self.set_local(auth('rt-a', now - 86400))
        r = self.run_ts('push')
        self.assertEqual((r.stdout, self.fake.puts), ('', 0))
        self.set_local(auth('rt-b', now))                   # CLI rotated it mid-run
        self.run_ts('push')
        self.assertEqual(self.fake.file['tokens']['refresh_token'], 'rt-b')

    def test_push_concurrent_newer_writer_is_detected_and_adopted(self):
        now = int(time.time())
        self.fake.file = auth('rt-a', now - 86400)
        self.set_local(auth('rt-b', now - 3600))

        def other_conversation_writes():   # lands right after our PUT
            self.fake.file = auth('rt-c', now)
        self.fake.after_put = other_conversation_writes
        r = self.run_ts('push')
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-c')
        self.assertEqual(self.fake.file['tokens']['refresh_token'], 'rt-c')
        # it adopted the other writer's token, so it must not claim it pushed its own
        self.assertIn('adopted', r.stdout)
        self.assertNotIn('pushed rotated token', r.stdout)

    def test_sync_concurrent_newer_writer_does_not_claim_store_updated(self):
        now = int(time.time())
        self.fake.file = auth('rt-a', now - 2 * 86400)
        self.set_local(auth('rt-b', now - 86400))

        def other_conversation_writes():
            self.fake.file = auth('rt-c', now)
        self.fake.after_put = other_conversation_writes
        r = self.run_ts('sync')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-c')
        self.assertIn('adopted', r.stdout)
        self.assertNotIn('store updated', r.stdout)

    def test_sync_dead_token_exits_relogin(self):
        now = int(time.time())
        self.set_local(auth('rt-old', now - 8 * 86400))
        self.fake.file = auth('rt-old', now - 8 * 86400)
        self.fake.valid_rt = 'rt-elsewhere'   # rejected, and no newer copy ever lands
        r = self.run_ts('sync')
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertIn('login flow required', r.stdout)
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-old')

    def test_push_never_uploads_api_key_auth(self):
        self.set_local(auth('rt-a', int(time.time()), api_key='sk-test'))
        self.run_ts('push')
        self.assertIsNone(self.fake.file)

    def test_recover(self):
        now = int(time.time())
        self.set_local(auth('rt-dead', now - 86400))
        self.fake.file = auth('rt-dead', now - 86400)
        self.assertEqual(self.run_ts('recover').returncode, 1)          # nothing better stored
        started = int(time.time()) - 5
        self.fake.file = auth('rt-new', now)
        self.assertEqual(self.run_ts('recover').returncode, 0)          # adopts newer copy
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-new')
        # post-run push already adopted it: still "retry", thanks to --since
        self.assertEqual(self.run_ts('recover', '--since', str(started)).returncode, 0)
        self.assertEqual(self.run_ts('recover', '--since', str(int(time.time()) + 60)).returncode, 1)

    def test_recover_does_not_adopt_older_stored_token(self):
        now = int(time.time())
        self.set_local(auth('rt-dead', now))
        self.fake.file = auth('rt-older', now - 3 * 86400)
        r = self.run_ts('recover')
        self.assertEqual(r.returncode, 1)
        self.assertIn('older', r.stdout)
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-dead')

    def test_corrupt_store_is_overwritten_by_valid_local(self):
        now = int(time.time())
        for broken in (b'{not json', [1, 2], {'tokens': {}}, auth('rt-x', now, api_key='sk-test')):
            for cmd in ('push', 'sync'):
                with self.subTest(broken=broken, cmd=cmd):
                    self.fake.file = broken
                    self.set_local(auth('rt-a', now - 3600))
                    r = self.run_ts(cmd)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertNotIn('Traceback', r.stderr)
                    self.assertEqual(self.fake.file['tokens']['refresh_token'], 'rt-a')

    def test_corrupt_store_status_and_recover_do_not_crash(self):
        self.fake.file = b'{not json'
        self.set_local(auth('rt-a', int(time.time())))
        r = self.run_ts('status')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('remote_issued=corrupt', r.stdout)
        r = self.run_ts('recover')
        self.assertEqual(r.returncode, 1)
        self.assertNotIn('Traceback', r.stderr)
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-a')

    def test_missing_boto3_is_a_warning_not_a_traceback(self):
        shadow = self.home.parent / 'noboto'
        for mod in ('boto3', 'botocore'):
            (shadow / mod).mkdir(parents=True)
            (shadow / mod / '__init__.py').write_text(
                'raise ModuleNotFoundError("No module named %r", name=%r)\n' % (mod, mod))
        self.env['PYTHONPATH'] = str(shadow)
        self.set_local(auth('rt-a', int(time.time())))
        for cmd, code in (('status', 0), ('push', 0), ('sync', 0), ('recover', 1)):
            with self.subTest(cmd=cmd):
                r = self.run_ts(cmd)
                self.assertEqual(r.returncode, code, r.stdout + r.stderr)
                self.assertNotIn('Traceback', r.stderr)
                self.assertIn('boto3', r.stdout)
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-a')

    def test_bad_credentials_are_a_warning_not_a_failure(self):
        self.set_local(auth('rt-a', int(time.time())))
        orig = Handler.do_GET
        Handler.do_GET = lambda h: h._s3_error(403, 'AccessDenied')
        self.addCleanup(setattr, Handler, 'do_GET', orig)
        r = self.run_ts('sync')
        self.assertEqual(r.returncode, 0)
        self.assertIn('warning', r.stdout)
        self.assertIn('AccessDenied', r.stdout)
        self.assertNotIn('test-secret', r.stdout + r.stderr)
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-a')

    def test_incomplete_credentials_file_is_a_warning(self):
        self.envfile.write_text(json.dumps({'endpoint': 'x', 'bucket': 'b'}))
        self.set_local(auth('rt-a', int(time.time())))
        r = self.run_ts('push')
        self.assertEqual(r.returncode, 0)
        self.assertIn('missing: key_id, application_key', r.stdout)

    def test_custom_object_key_and_bare_host_endpoint_parsing(self):
        global KEY_PATH
        old = KEY_PATH
        KEY_PATH = '/tokens/secret/dir/auth.json'
        self.addCleanup(lambda: globals().__setitem__('KEY_PATH', old))
        self.write_creds(object_key='/secret/dir/auth.json')
        self.set_local(auth('rt-a', int(time.time())))
        self.run_ts('sync')
        self.assertEqual(self.fake.file['tokens']['refresh_token'], 'rt-a')

    # --- end-to-end through setup.sh / ask.sh with a mock codex binary --------

    def mock_codex(self, body='print("answer")\n'):
        bindir = self.home.parent / 'bin'
        bindir.mkdir(exist_ok=True)
        cli = bindir / 'codex'
        cli.write_text('#!/usr/bin/env python3\nimport json, os, sys, time\n'
                       'if sys.argv[1:2] in (["--version"], ["login"]): print("mock"); sys.exit(0)\n'
                       'open(os.environ["CODEX_HOME"] + "/exec_calls", "a").write("x\\n")\n' + body)
        cli.chmod(0o700)
        return dict(self.env, PATH=str(bindir) + os.pathsep + os.environ['PATH'])

    def skill_copy(self, bundled):
        """A copy of the skill whose auth/ holds a bundled auth.json and the
        token-store credentials (setup.sh gates the store on that file)."""
        dst = self.home.parent / 'skill'
        shutil.copytree(SCRIPT.parents[1], dst, ignore=shutil.ignore_patterns('__pycache__'))
        (dst / 'auth' / 'auth.json').write_text(json.dumps(bundled))
        shutil.copy(self.envfile, dst / 'auth' / 'credentials.json')
        return dst

    def test_setup_dead_token_starts_login_immediately(self):
        now = int(time.time())
        dead = auth('rt-old', now - 8 * 86400)
        self.fake.file = dead
        self.fake.valid_rt = 'rt-elsewhere'
        skill = self.skill_copy(dead)
        env = self.mock_codex()
        r = subprocess.run(['sh', str(skill / 'scripts/setup.sh')], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('refresh token was rejected', r.stdout)
        self.assertIn('auth.openai.com/oauth/authorize', r.stdout)
        self.assertFalse((self.home / 'auth.json').exists())
        self.assertTrue((self.home / 'auth.json.dead').exists())
        # ask.sh refuses to touch the dead token (exit 3) instead of failing
        # against it; a second setup run must not re-seed the dead bundled copy
        (skill / 'auth' / 'credentials.json').unlink()
        r = subprocess.run(['sh', str(skill / 'scripts/ask.sh'), 'hi', 'low', 'gpt-test'], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertFalse((self.home / 'exec_calls').exists())

    def test_setup_healthy_token_does_not_start_login(self):
        now = int(time.time())
        self.fake.file = auth('rt-stored', now - 86400)
        skill = self.skill_copy(auth('rt-bundled', now - 9 * 86400))
        r = subprocess.run(['sh', str(skill / 'scripts/setup.sh')], env=self.mock_codex(),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('oauth/authorize', r.stdout)
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-stored')
        self.assertFalse((self.home / 'auth.json.dead').exists())

    def run_ask(self, codex_body):
        bindir = self.home.parent / 'bin'
        bindir.mkdir(exist_ok=True)
        cli = bindir / 'codex'
        cli.write_text('#!/usr/bin/env python3\nimport json, os, sys, time\n' + codex_body)
        cli.chmod(0o700)
        env = dict(self.env, PATH=str(bindir) + os.pathsep + os.environ['PATH'])
        ask = SCRIPT.parent / 'ask.sh'
        return subprocess.run(['sh', str(ask), 'hi', 'low', 'gpt-test'], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_ask_pushes_token_rotated_by_cli_during_run(self):
        now = int(time.time())
        self.fake.file = auth('rt-a', now - 86400)
        self.set_local(auth('rt-a', now - 86400))
        body = (
            'p = os.environ["CODEX_HOME"] + "/auth.json"\n'
            'd = json.load(open(p)); d["tokens"]["refresh_token"] = "rt-cli"\n'
            'd["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())\n'
            'json.dump(d, open(p, "w")); print("answer")\n')
        r = self.run_ask(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fake.file['tokens']['refresh_token'], 'rt-cli')

    def test_ask_dead_token_recovers_from_store_instead_of_relogin(self):
        now = int(time.time())
        self.set_local(auth('rt-dead', now - 86400))
        self.fake.file = auth('rt-new', now)
        r = self.run_ask('print("ERROR: Your access token could not be refreshed."); sys.exit(1)\n')
        self.assertIn('auth_status=recovered', r.stdout)
        self.assertNotIn('auth.openai.com/oauth/authorize', r.stdout)
        self.assertEqual(self.local()['tokens']['refresh_token'], 'rt-new')


if __name__ == '__main__':
    unittest.main()
