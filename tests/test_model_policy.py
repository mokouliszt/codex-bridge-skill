"""Offline CLI-boundary checks: python3 -m unittest discover -s tests -v."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/codex-bridge/scripts'
MOCK = '''#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:] == ['app-server']:
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get('method') == 'initialize':
            print(json.dumps({'id': 0, 'result': {}}), flush=True)
        elif msg.get('method') == 'model/list':
            print(json.dumps({'id': 1, 'result': {'data': json.loads(os.environ['TEST_MODELS'])}}), flush=True)
else:
    print('mock codex: ' + ' '.join(sys.argv[1:]))
'''


def model(mid, efforts=('low', 'medium'), **extra):
    return dict(id=mid, supportedReasoningEfforts=[{'reasoningEffort': e} for e in efforts], **extra)


class ModelPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cli = self.root / 'codex'
        cli.write_text(MOCK)
        cli.chmod(0o700)
        self.state = self.root / 'state'
        self.state.mkdir()
        (self.state / 'auth.json').write_text('{"OPENAI_API_KEY": null}')
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                        CODEX_HOME=str(self.state), CODEX_DRYRUN='1')
        self.env.pop('CODEX_MODEL', None)

    def run_script(self, name, *args, code=0):
        result = subprocess.run(['sh', str(SCRIPTS / name), *args], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout

    def test_defaults_override_legacy_state_for_new_and_resumed_calls(self):
        (self.state / 'model_cache').write_text('gpt-5.6-sol\n')
        (self.state / 'config.toml').write_text('model = "gpt-5.6-sol"\nmodel_reasoning_effort = "ultra"\n')
        for args in [('hello',), ('--continue', 'hello'), ('--resume', 'test-session', 'hello')]:
            with self.subTest(args=args):
                out = self.run_script('ask.sh', *args)
                self.assertIn('-m gpt-6-astra', out)
                self.assertIn('model_reasoning_effort=medium', out)
                if args[0] == '--continue':
                    self.assertIn('exec resume --last', out)
                if args[0] == '--resume':
                    self.assertIn('exec resume test-session', out)

    def test_effort_overrides(self):
        for effort in ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
            with self.subTest(effort=effort):
                out = self.run_script('ask.sh', 'hello', effort)
                self.assertIn('model_reasoning_effort=' + effort, out)
                self.assertIn('-m gpt-6-astra', out)

    def test_explicit_model_and_positional_precedence(self):
        self.env['CODEX_MODEL'] = 'gpt-5.6-sol'
        out = self.run_script('ask.sh', 'hello')
        self.assertIn('-m gpt-5.6-sol', out)
        self.assertIn('model_reasoning_effort=medium', out)
        out = self.run_script('ask.sh', '--continue', 'hello', 'xhigh', 'gpt-5.6-luna')
        self.assertIn('-m gpt-5.6-luna', out)
        self.assertIn('model_reasoning_effort=xhigh', out)

    def test_invalid_effort_does_not_start_exec(self):
        for effort in ('typo', 'MEDIUM'):
            self.assertNotIn('DRYRUN:', self.run_script('ask.sh', 'hello', effort, code=2))
        self.assertFalse((self.state / 'bg').exists())

    def test_setup_writes_defaults_and_preserves_existing_config(self):
        self.run_script('setup.sh')
        config = self.state / 'config.toml'
        self.assertIn('model = "gpt-6-astra"', config.read_text())
        self.assertIn('model_reasoning_effort = "medium"', config.read_text())
        legacy = 'model = "gpt-5.6-sol"\n# user custom settings\n'
        config.write_text(legacy)
        self.run_script('setup.sh')
        self.assertEqual(config.read_text(), legacy)
        self.assertFalse((self.state / 'model_cache').exists())

    def test_resolve_pins_astra_even_with_newer_default(self):
        self.env['TEST_MODELS'] = json.dumps([model('gpt-7.0-sol', ('xhigh',), isDefault=True), model('gpt-6-astra')])
        self.assertEqual(self.run_script('models.sh', 'resolve').strip(), 'gpt-6-astra')
        self.assertIn('gpt-7.0-sol', self.run_script('models.sh', 'list'))

    def test_resolve_never_falls_back(self):
        for candidate in (model('gpt-5.6-sol'), model('gpt-6-astra', hidden=True), model('gpt-6-astra', ('medium',))):
            with self.subTest(candidate=candidate):
                self.env['TEST_MODELS'] = json.dumps([candidate])
                self.assertEqual(self.run_script('models.sh', 'resolve', code=1), '')


if __name__ == '__main__':
    unittest.main()
