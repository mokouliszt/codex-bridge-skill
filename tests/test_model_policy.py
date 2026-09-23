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

    def test_missing_model_or_effort_refuses_without_side_effects(self):
        self.env['TEST_MODELS'] = '[]'
        for args in [('hello',), ('hello', 'medium'), ('--continue', 'hello'),
                     ('--resume', 'test-session', 'hello', 'high')]:
            with self.subTest(args=args):
                result = subprocess.run(['sh', str(SCRIPTS / 'ask.sh'), *args], env=self.env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn('DRYRUN:', result.stdout)
                self.assertIn('models.sh', result.stderr)
                self.assertIn('choices', result.stderr)
        self.env['CODEX_MODEL'] = 'gpt-6-sol'
        self.assertNotIn('DRYRUN:', self.run_script('ask.sh', 'hello', code=2))
        self.assertFalse((self.state / 'bg').exists())

    def test_explicit_choice_overrides_legacy_state_for_new_and_resumed_calls(self):
        (self.state / 'model_cache').write_text('gpt-5.6-sol\n')
        (self.state / 'config.toml').write_text('model = "gpt-5.6-sol"\nmodel_reasoning_effort = "ultra"\n')
        for args in [('hello',), ('--continue', 'hello'), ('--resume', 'test-session', 'hello')]:
            with self.subTest(args=args):
                out = self.run_script('ask.sh', *args, 'medium', 'gpt-6-luna')
                self.assertIn('-m gpt-6-luna', out)
                self.assertIn('model_reasoning_effort=medium', out)
                if args[0] == '--continue':
                    self.assertIn('exec resume --last', out)
                if args[0] == '--resume':
                    self.assertIn('exec resume test-session', out)

    def test_all_efforts_accepted(self):
        for effort in ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
            with self.subTest(effort=effort):
                out = self.run_script('ask.sh', 'hello', effort, 'gpt-6-sol')
                self.assertIn('model_reasoning_effort=' + effort, out)
                self.assertIn('-m gpt-6-sol', out)

    def test_env_model_and_positional_precedence(self):
        self.env['CODEX_MODEL'] = 'gpt-5.6-sol'
        out = self.run_script('ask.sh', 'hello', 'low')
        self.assertIn('-m gpt-5.6-sol', out)
        self.assertIn('model_reasoning_effort=low', out)
        out = self.run_script('ask.sh', '--continue', 'hello', 'xhigh', 'gpt-5.6-luna')
        self.assertIn('-m gpt-5.6-luna', out)
        self.assertIn('model_reasoning_effort=xhigh', out)

    def test_invalid_effort_does_not_start_exec(self):
        for effort in ('typo', 'MEDIUM'):
            self.assertNotIn('DRYRUN:', self.run_script('ask.sh', 'hello', effort, 'gpt-6-sol', code=2))
        self.assertFalse((self.state / 'bg').exists())

    def test_setup_writes_no_default_model_and_preserves_existing_config(self):
        self.run_script('setup.sh')
        config = (self.state / 'config.toml').read_text()
        self.assertNotIn('model =', config)
        self.assertNotIn('model_reasoning_effort', config)
        self.assertIn('web_search = true', config)
        legacy = 'model = "gpt-5.6-sol"\n# user custom settings\n'
        (self.state / 'config.toml').write_text(legacy)
        self.run_script('setup.sh')
        self.assertEqual((self.state / 'config.toml').read_text(), legacy)
        self.assertFalse((self.state / 'model_cache').exists())

    def test_choices_ranks_strongest_first(self):
        # deliberately shuffled; includes a hidden model and an unsuffixed one
        self.env['TEST_MODELS'] = json.dumps([
            model('gpt-5.6-luna'), model('gpt-5.5'), model('gpt-6-luna'),
            model('gpt-5.6-terra'), model('gpt-6-sol'), model('gpt-5.6-sol'),
            model('gpt-6-astra', ('low', 'medium', 'ultra')), model('gpt-6-secret', hidden=True),
        ])
        out = self.run_script('models.sh', 'choices')
        ids = [line.split()[1] for line in out.splitlines()]
        self.assertEqual(ids, ['gpt-6-astra', 'gpt-6-sol', 'gpt-5.6-sol', 'gpt-5.6-terra',
                               'gpt-6-luna', 'gpt-5.6-luna', 'gpt-5.5'])
        self.assertIn('efforts=low/medium/ultra', out.splitlines()[0])
        # list keeps the raw CLI order and still shows everything returned
        self.assertIn('gpt-5.6-luna', self.run_script('models.sh', 'list').splitlines()[0])

    def test_choices_newer_minor_generation_first(self):
        self.env['TEST_MODELS'] = json.dumps([model('gpt-6-sol'), model('gpt-6.1-sol'), model('gpt-7-luna')])
        ids = [l.split()[1] for l in self.run_script('models.sh', 'choices').splitlines()]
        self.assertEqual(ids, ['gpt-6.1-sol', 'gpt-6-sol', 'gpt-7-luna'])

    def test_resolve_mode_removed(self):
        self.env['TEST_MODELS'] = json.dumps([model('gpt-6-astra')])
        self.assertEqual(self.run_script('models.sh', 'resolve', code=1), '')


if __name__ == '__main__':
    unittest.main()
