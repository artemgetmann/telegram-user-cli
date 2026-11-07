"""Agent binding tests use synthetic config and paths, never an account transport."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from telegram_user_cli import binding, cli, backend


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = self.root / 'binding.json'
        self.data = {'version': 1, 'env_file': str(self.root / 'credential fixture.env'),
                     'session': str(self.root / 'account Ю.session'),
                     'shared_locks': [str(self.root / 'machine.lock'), str(self.root / 'legacy.lock')]}
        self.config.write_text(json.dumps(self.data))
        self.environment = {'HOME': str(self.root), 'TELEGRAM_USER_BINDING': str(self.config)}

    def run_main(self, argv, *, environment=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, environment or self.environment, clear=True), \
                patch.object(sys, 'argv', ['telegram-user-bound', *argv]), \
                patch.object(binding.os, 'execve') as execute, redirect_stdout(out), redirect_stderr(err):
            before = dict(os.environ)
            code = binding.main()
            self.assertEqual(dict(os.environ), before)
        return code, out.getvalue(), err.getvalue(), execute

    def test_selected_binding_and_all_locks_cannot_be_weakened_by_environment(self):
        env = dict(self.environment, TELEGRAM_USER_SESSION='/wrong.session',
                   TELEGRAM_USER_LOCK_PATH='/wrong.lock', TELEGRAM_API_ID='999',
                   TELEGRAM_API_HASH='fixture-ambient', UNRELATED='keep me')
        for key in ('OPENCLAW_HOME', 'OPENCLAW_STATE_DIR', 'OPENCLAW_CONFIG_PATH',
                    'OPENCLAW_LOG_DIR', 'OPENCLAW_PROFILE', 'OPENCLAW_LAUNCHD_LABEL'):
            env[key] = 'fixture unchanged ' + key
        code, out, err, execute = self.run_main(['inbox', '--json'], environment=env)
        self.assertEqual((code, out, err), (0, '', ''))
        execute.assert_called_once()
        executable, argv, child = execute.call_args.args
        self.assertEqual(executable, sys.executable)
        self.assertEqual(argv[:2], [sys.executable, '-c'])
        self.assertEqual(argv[3], str(Path(binding.__file__).resolve().parent.parent))
        parsed = cli.build_parser().parse_args(cli.normalize_globals(argv[4:]))
        self.assertEqual(parsed.session, self.data['session'])
        self.assertEqual(parsed.env_file, self.data['env_file'])
        self.assertEqual(parsed.lock, self.data['session'] + '.lock')
        self.assertEqual(parsed.shared_lock, self.data['shared_locks'])
        with patch.dict(os.environ, child, clear=True):
            locks = backend.resolve_session_lock_paths(Path(parsed.session), parsed.lock,
                                                       shared_locks=parsed.shared_lock)
        self.assertEqual(set(locks), {Path(parsed.lock), *map(Path, parsed.shared_lock)})
        self.assertEqual(child, {k: v for k, v in env.items()
                                 if k not in ('TELEGRAM_API_ID', 'TELEGRAM_API_HASH')})
        self.assertNotIn('/wrong.lock', list(map(str, locks)))

    def test_argument_tokens_preserved_with_unicode_spaces_option_payload_and_separator(self):
        cases = [
            ['send', '--chat', 'fixture', '--message', 'hello Юникод with spaces'],
            ['send', '--chat', 'fixture', '--message-file', '/fixture/space Ю.txt'],
            ['send', '--chat', 'fixture', '--message=--session'],
            ['send', '--chat', 'fixture', '--message=--shared-lock=/literal'],
            ['wait', '--chat', 'fixture', '--after-id', '12', '--timeout', '0.5'],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, _, _, execute = self.run_main(argv)
                self.assertEqual(code, 0)
                self.assertEqual(execute.call_args.args[1][-len(argv):], argv)

    def test_leading_separator_matches_underlying_parser(self):
        argv = ['--', 'inbox']
        try:
            cli.build_parser().parse_args(cli.normalize_globals(argv))
        except ValueError:
            code, out, err, execute = self.run_main(argv)
            self.assertEqual((code, out), (2, ''))
            self.assertEqual(json.loads(err)['error']['code'], 'E_USAGE')
            execute.assert_not_called()
        else:
            code, out, err, execute = self.run_main(argv)
            self.assertEqual((code, out, err), (0, '', ''))
            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[1][-len(argv):], argv)

    def test_caller_selector_overrides_and_abbreviations_fail_before_exec(self):
        cases = [
            ['inbox', '--session', '/wrong.session'],
            ['--session=/wrong.session', 'inbox'],
            ['inbox', '--env-file=/wrong.env'],
            ['--env-f', '/wrong.env', 'inbox'],
            ['--sess', '/wrong.session', 'inbox'],
            ['inbox', '--lock', '/wrong.lock'],
            ['inbox', '--shared-lock=/extra.lock'],
            ['inbox', '--shared-lock='],
            ['inbox', '--ses', '/wrong.session'],
            ['--', 'inbox', '--session', '/wrong.session'],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, err, execute = self.run_main(argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertIn(json.loads(err)['error']['code'], ('E_BINDING_OVERRIDE', 'E_USAGE'))
                execute.assert_not_called()

    def test_help_version_and_command_help_are_noninteractive_and_need_no_config(self):
        self.config.unlink()
        for argv in ([], ['--help'], ['--version']):
            code, out, err, execute = self.run_main(argv)
            self.assertEqual((code, err), (0, ''))
            self.assertTrue(out)
            execute.assert_not_called()
        code, out, _, _ = self.run_main(['--help'])
        self.assertIn('TELEGRAM_USER_BINDING', out)
        self.assertIn('--check-binding', out)
        self.assertIn('shared_locks', out)
        with patch.dict(os.environ, self.environment, clear=True), \
                patch.object(sys, 'argv', ['bound', 'inbox', '--help']), redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            binding.main()
        self.assertEqual(caught.exception.code, 0)

    def test_missing_invalid_duplicate_oversize_and_secret_config_fail_without_echo(self):
        payloads = [None, '{', '[]', json.dumps(dict(self.data, api_hash='DO_NOT_ECHO')),
                    json.dumps(dict(self.data, session='relative.session')),
                    json.dumps(dict(self.data, session='/missing-suffix')),
                    json.dumps(dict(self.data, version=True)),
                    json.dumps(dict(self.data, shared_locks=None)),
                    json.dumps(dict(self.data, shared_locks=['relative'])),
                    json.dumps(dict(self.data, env_file='/control\npath')),
                    '{"version":1,"version":1}', ' ' * 16_385]
        for payload in payloads:
            with self.subTest(payload=payload):
                if payload is None:
                    self.config.unlink(missing_ok=True)
                else:
                    self.config.write_text(payload)
                code, out, err, execute = self.run_main(['inbox'])
                self.assertEqual((code, out), (2, ''))
                self.assertEqual(json.loads(err)['error']['code'], 'E_BINDING_CONFIG')
                self.assertNotIn('DO_NOT_ECHO', err)
                execute.assert_not_called()

    def test_check_binding_opens_no_selected_targets_and_default_is_one_xdg_file(self):
        code, out, err, execute = self.run_main(['--check-binding'])
        self.assertEqual((code, err), (0, ''))
        result = json.loads(out)
        self.assertEqual(result['binding'], self.data)
        self.assertEqual(result['per_session_lock'], self.data['session'] + '.lock')
        self.assertEqual(list(self.root.iterdir()), [self.config])
        execute.assert_not_called()
        directory = self.root / 'configuration' / 'telegram-user'
        directory.mkdir(parents=True)
        self.config.replace(directory / 'binding.json')
        env = {'HOME': str(self.root), 'XDG_CONFIG_HOME': str(directory.parent)}
        self.assertEqual(self.run_main(['--check-binding'], environment=env)[0], 0)
        env['TELEGRAM_USER_BINDING'] = 'relative.json'
        self.assertEqual(self.run_main(['--check-binding'], environment=env)[0], 2)

    def test_config_failure_explains_safe_fix_without_echoing_values(self):
        self.config.unlink()
        self.assertIn('file not found', json.loads(self.run_main(['inbox'])[2])['error']['message'])
        self.config.write_text(json.dumps(dict(self.data, session='DO_NOT_ECHO')))
        error = self.run_main(['--check-binding'])[2]
        self.assertIn('absolute paths', error)
        self.assertNotIn('DO_NOT_ECHO', error)
        self.config.write_text('{DO_NOT_ECHO')
        error = self.run_main(['inbox'])[2]
        self.assertIn('valid JSON', error)
        self.assertNotIn('DO_NOT_ECHO', error)

    def test_exec_failure_is_structured_and_never_retried(self):
        with patch.dict(os.environ, self.environment, clear=True), \
                patch.object(sys, 'argv', ['bound', 'inbox']), \
                patch.object(binding.os, 'execve', side_effect=OSError('DO_NOT_ECHO')) as execute, \
                redirect_stderr(io.StringIO()) as error:
            self.assertEqual(binding.main(), 2)
        execute.assert_called_once()
        self.assertEqual(json.loads(error.getvalue())['error']['code'], 'E_BINDING_EXEC')
        self.assertNotIn('DO_NOT_ECHO', error.getvalue())

    def subprocess_environment(self):
        # Existing source only, no installation; environment and all paths synthetic.
        return dict(self.environment, PATH=os.environ.get('PATH', ''),
                    PYTHONDONTWRITEBYTECODE='1',
                    PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'),
                    TELEGRAM_API_ID='999', TELEGRAM_API_HASH='ambient-fixture')

    def test_real_exec_keeps_pid_exit_status_and_forwards_stdin(self):
        stub = self.root / 'probe.py'
        stub.write_text("import os,sys,json; print(json.dumps({'pid':os.getpid(),'argv':sys.argv[1:],'stdin':sys.stdin.read(),'ambient_hash':'TELEGRAM_API_HASH' in os.environ})); sys.exit(7)")
        code = textwrap.dedent(f'''
            import os,sys
            from telegram_user_cli import binding
            original=os.execve
            print(os.getpid(),flush=True)
            def probe(executable, argv, env):
                original(executable,[executable,{str(stub)!r},*argv[4:]],env)
            binding.os.execve=probe
            sys.argv=['bound','inbox']
            raise SystemExit(binding.main())
        ''')
        result = subprocess.run([sys.executable, '-c', code], input='exact Ю\n',
                                env=self.subprocess_environment(), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 7, result.stderr)
        pid, output = result.stdout.splitlines()
        data = json.loads(output)
        self.assertEqual(data['pid'], int(pid))
        self.assertEqual(data['argv'][-1], 'inbox')
        self.assertEqual(data['stdin'], 'exact Ю\n')
        self.assertFalse(data['ambient_hash'])

    def test_real_child_doctor_uses_only_selected_fake_env_credentials(self):
        Path(self.data['env_file']).write_text('TELEGRAM_API_ID=123\n')  # hash deliberately absent
        result = subprocess.run([sys.executable, '-m', 'telegram_user_cli.binding', 'doctor', '--json'],
                                env=self.subprocess_environment(), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['state'], 'missing_credentials')
        self.assertFalse(Path(self.data['session']).exists())
        self.assertFalse(Path(self.data['session'] + '.lock').exists())

    def test_console_launcher_pins_selected_package_despite_shadow_cwd(self):
        # Exercise actual exec with both site-packages-like and src layouts.
        import shutil

        hostile = self.root / 'caller cwd'
        shadow = hostile / 'telegram_user_cli'
        shadow.mkdir(parents=True)
        (shadow / '__init__.py').write_text('')
        (shadow / '__main__.py').write_text("raise SystemExit('SHADOW EXECUTED')")
        source = Path(binding.__file__).resolve().parent
        for layout in ('site-packages', 'src'):
            with self.subTest(layout=layout):
                selected = self.root / layout
                shutil.copytree(source, selected / 'telegram_user_cli')
                launcher = self.root / (layout + '-console.py')
                launcher.write_text(
                    "import sys; sys.path.insert(0," + repr(str(selected)) + "); "
                    "from telegram_user_cli.binding import main; raise SystemExit(main())")
                Path(self.data['env_file']).write_text('TELEGRAM_API_ID=123\n')
                env = self.subprocess_environment()
                # Do not let a globally reordered PYTHONPATH hide the regression.
                env.pop('PYTHONPATH')
                result = subprocess.run(
                    [sys.executable, str(launcher), 'doctor', '--json'], cwd=hostile,
                    env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['state'], 'missing_credentials')
                self.assertNotIn('SHADOW', result.stdout + result.stderr)
                self.assertFalse(Path(self.data['session']).exists())

    def test_caller_deadline_terminates_exec_process_without_a_supervisor(self):
        stub = self.root / 'sleep.py'
        stub.write_text('import time; time.sleep(30)')
        code = ("import os,sys; from telegram_user_cli import binding; original=os.execve; "
                f"binding.os.execve=lambda path,argv,env: original(path,[path,{str(stub)!r}],env); "
                "sys.argv=['bound','inbox']; binding.main()")
        process = subprocess.Popen([sys.executable, '-c', code], env=self.subprocess_environment(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.5)
        finally:
            process.kill()
            process.communicate(timeout=5)
        self.assertLess(process.returncode, 0)


if __name__ == '__main__':
    unittest.main()
