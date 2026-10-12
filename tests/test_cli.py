"""Operator behavior through parsing, dispatch, and the installed module interface."""

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from telethon import TelegramClient

from telegram_user_cli import backend, cli


class CliTests(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(cli.normalize_globals(list(argv)))

    def test_globals_work_after_command_and_do_not_rewrite_payload(self):
        args = self.parse("send", "--chat", "me", "--message=--session",
                          "--session", "/tmp/example", "--json")
        self.assertEqual(args.message, "--session")
        self.assertEqual(args.session, "/tmp/example")
        self.assertTrue(args.json)

    def test_env_file_is_explicit_and_preserves_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "credentials"
            file.write_text("TELEGRAM_API_ID=123\nTELEGRAM_API_HASH=fixture\n", encoding="utf-8")
            with patch.dict(os.environ, {"TELEGRAM_API_ID": "456"}, clear=True):
                cli.load_credentials(str(file))
                self.assertEqual(os.environ["TELEGRAM_API_ID"], "456")
                self.assertEqual(os.environ["TELEGRAM_API_HASH"], "fixture")

    def test_does_not_auto_discover_dotenv(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(cli, "load_dotenv") as loader:
            cli.load_credentials(None)
            loader.assert_not_called()

    def test_doctor_does_not_create_session_or_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "new" / "user"
            args = self.parse("doctor", "--session", str(session))
            out = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), redirect_stdout(out), \
                    patch.object(backend, "create_telegram_client") as client:
                self.assertEqual(cli.doctor(args), 0)
            self.assertEqual(json.loads(out.getvalue())["state"], "missing_credentials")
            self.assertFalse(session.parent.exists())
            client.assert_not_called()

    def test_doctor_file_exists_is_not_authentication_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "user.session"
            session.touch()
            args = self.parse("doctor", "--session", str(session))
            out = io.StringIO()
            with patch.dict(os.environ, {"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "fixture"},
                            clear=True), redirect_stdout(out):
                cli.doctor(args)
            self.assertEqual(json.loads(out.getvalue())["state"], "auth_unverified")

    def test_wait_returns_only_new_inbound_from_selected_sender(self):
        args = self.parse("wait", "--chat", "me", "--after-id", "10", "--sender-id", "123")
        result = {"messages": [
            {"message_id": 11, "out": True, "sender_id": 123},
            {"message_id": 12, "out": False, "sender_id": 456},
            {"message_id": 13, "out": False, "sender_id": 123, "text": "reply"},
        ]}
        out = io.StringIO()
        with patch.object(cli, "capture", AsyncMock(return_value=(0, result))), redirect_stdout(out):
            self.assertEqual(asyncio.run(cli.wait_for_message(args)), 0)
        self.assertEqual([m["message_id"] for m in json.loads(out.getvalue())["messages"]], [13])

    def test_wait_timeout_is_bounded_and_does_not_send(self):
        args = self.parse("wait", "--chat", "me", "--after-id", "10", "--timeout", "0.01")
        out = io.StringIO()
        with patch.object(cli, "capture", AsyncMock(return_value=(0, {"messages": []}))), \
                patch.object(backend, "run_send") as sender, redirect_stdout(out):
            self.assertEqual(asyncio.run(cli.wait_for_message(args)), 0)
        self.assertTrue(json.loads(out.getvalue())["timed_out"])
        sender.assert_not_called()

    def test_wait_pages_past_one_hundred_nonmatching_messages(self):
        args = self.parse("wait", "--chat", "me", "--after-id", "10", "--sender-id", "123",
                          "--contains", "reply")
        newest = {"messages": [{"message_id": message_id, "out": True, "sender_id": 123}
                               for message_id in range(111, 11, -1)]}
        older = {"messages": [{"message_id": 11, "out": False, "sender_id": 123,
                                "text": "OLDER REPLY"}]}
        cursors = []

        async def page(namespace):
            cursors.append(namespace.before_id)
            self.assertEqual(namespace.contains, "")
            return 0, newest if namespace.before_id == 0 else older

        out = io.StringIO()
        with patch.object(cli, "capture", side_effect=page), redirect_stdout(out):
            self.assertEqual(asyncio.run(cli.wait_for_message(args)), 0)
        self.assertEqual(cursors, [0, 12])
        self.assertEqual(json.loads(out.getvalue())["messages"][0]["message_id"], 11)

    def test_operation_timeout_reports_uncertainty_and_does_not_retry(self):
        args = self.parse("send", "--chat", "me", "--message", "hello", "--rpc-timeout", "0.01")

        async def stalled(_):
            await asyncio.sleep(1)

        err = io.StringIO()
        with patch.object(backend, "run", AsyncMock(side_effect=stalled)) as runner, redirect_stderr(err):
            self.assertEqual(asyncio.run(cli.invoke(args)), 1)
        runner.assert_awaited_once()
        self.assertEqual(json.loads(err.getvalue())["error"]["code"], "E_TIMEOUT")

    def test_real_telethon_constructor_accepts_transport_options(self):
        # Exercise the real library boundary. Replacing the whole factory would
        # hide invalid constructor keywords even while all fake-client tests pass.
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(backend, "resolve_api_credentials", return_value=(123, "fixture")), \
                patch.object(TelegramClient, "connect", AsyncMock()), \
                patch.object(TelegramClient, "is_user_authorized", AsyncMock(return_value=True)), \
                patch.object(TelegramClient, "get_me", AsyncMock(return_value=object())), \
                patch.object(TelegramClient, "disconnect", AsyncMock()):
            async def check():
                client, _ = await backend.connect_client(Path(directory) / "user.session")
                self.assertIsNone(client.parse_mode)
                await client.disconnect()
                client.session.close()
            asyncio.run(check())

    def test_interactive_login_uses_stdin_for_code_and_password(self):
        args = self.parse("login", "--phone", "+15550000000")
        observed = []

        async def stage(namespace):
            observed.append((namespace.secret_stdin, sys.stdin.read() if namespace.secret_stdin else None))
            index = len(observed)
            return 0, {"state": ["awaiting_code", "awaiting_password", "ready"][index - 1]}

        out = io.StringIO()
        with patch.object(cli, "capture", side_effect=stage), \
                patch.object(cli.getpass, "getpass", side_effect=["12345", "private-password"]), \
                redirect_stdout(out):
            self.assertEqual(asyncio.run(cli.interactive_login(args)), 0)
        self.assertEqual(observed, [(None, None), ("code", "12345\n"),
                                    ("password", "private-password\n")])
        self.assertEqual(json.loads(out.getvalue()), {"state": "ready"})
        self.assertNotIn("private-password", out.getvalue())

    def test_nan_deadline_is_rejected(self):
        with self.assertRaises((ValueError, argparse.ArgumentError, SystemExit)):
            self.parse("status", "--rpc-timeout", "nan")

    def test_module_invalid_command_returns_json_error_without_traceback(self):
        result = subprocess.run([sys.executable, "-m", "telegram_user_cli", "nonexistent"],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "E_USAGE")

    def test_module_missing_env_file_never_echoes_credentials(self):
        result = subprocess.run([sys.executable, "-m", "telegram_user_cli", "status",
                                 "--env-file", "/missing/example-env-file"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "E_USAGE")


if __name__ == "__main__":
    unittest.main()
