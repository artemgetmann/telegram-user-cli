"""Offline contract checks for transport validation and fake-client behavior."""
import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram_user_cli import backend as b


class BackendTests(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory(dir="/private/tmp")
    self.addCleanup(self.directory.cleanup)
    self.session = str(Path(self.directory.name) / "user.session")

  def args(self, *argv):
    return b.build_parser().parse_args(["--session", self.session, *argv])

  def invoke(self, args):
    output, error = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
      status = asyncio.run(b.run(args))
    return status, output.getvalue(), error.getvalue()

  def test_invalid_bounds_before_credentials_or_client(self):
    for flags in (("--limit", "0"), ("--limit", "201"), ("--topic-anchor", "0")):
      with self.subTest(flags=flags), patch.object(b, "resolve_api_credentials") as credentials:
        code, output, error = self.invoke(self.args("read", "--chat", "123", *flags))
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertEqual(json.loads(error)["error"]["code"], "E_USAGE")
        credentials.assert_not_called()

  def test_invalid_send_sources_before_client(self):
    combinations = [
      ["--message", "a", "--message-file", "x"],
      ["--caption", "a"],
      ["--message", "a", "--caption", "b", "--media", "x"],
      ["--message", "a", "--reply-to", "1", "--topic-anchor", "2"],
      ["--message", "a", "--voice"],
    ]
    for flags in combinations:
      with self.subTest(flags=flags), patch.object(b, "connect_client") as connect:
        self.assertEqual(self.invoke(self.args("send", "--chat", "123", *flags))[0], 1)
        connect.assert_not_called()

  def test_message_file_preserves_utf8_crlf_and_whitespace(self):
    path = Path(self.directory.name) / "payload.txt"
    payload = "  Hello **world**\r\nПривет\n "
    path.write_bytes(payload.encode("utf-8"))
    self.assertEqual(b.resolve_text_payload(self.args("send", "--chat", "123", "--message-file", str(path)), "message"), payload)

  def test_stdin_dash_and_alias(self):
    for flag in (["--message-file", "-"], ["--message-stdin"]):
      with patch.object(b.sys, "stdin", io.StringIO("  a\nb\n")):
        self.assertEqual(b.resolve_text_payload(self.args("send", "--chat", "123", *flag), "message"), "  a\nb\n")

  def test_inline_newline_ambiguity_rejected(self):
    for text in ("a\nb", "a\rb", "a\tb", "a\\nb", "a\\rb", "a\\b", "a\\tb"):
      with self.subTest(text=text), self.assertRaises(ValueError):
        b.resolve_text_payload(self.args("send", "--chat", "123", "--message", text), "message")

  def test_oversize_and_invalid_utf8_rejected(self):
    path = Path(self.directory.name) / "payload.txt"
    for payload in (b"x" * (b.MAX_INPUT_BYTES + 1), b"\xff"):
      path.write_bytes(payload)
      with self.assertRaises(ValueError):
        b.resolve_text_payload(self.args("send", "--chat", "123", "--message-file", str(path)), "message")

  def fake_client(self, *, ack_failure=False):
    message = SimpleNamespace(id=123, chat_id=123, message="a", get_chat=AsyncMock(return_value=SimpleNamespace(title="Generic")))
    client = SimpleNamespace(send_message=AsyncMock(return_value=message), send_file=AsyncMock(return_value=message), send_read_acknowledge=AsyncMock(), disconnect=AsyncMock())
    if ack_failure:
      client.send_read_acknowledge.side_effect = RuntimeError("failed")
    return client

  def test_fake_send_exact_text_and_ack(self):
    path = Path(self.directory.name) / "message.txt"
    path.write_bytes(b"  exact\r\n**text**\n")
    client = self.fake_client()
    with patch.object(b, "resolve_api_credentials", return_value=(123, "fake")), patch.object(b, "connect_client", AsyncMock(return_value=(client, None))):
      status, output, error = self.invoke(self.args("send", "--chat", "123", "--message-file", str(path)))
    self.assertEqual((status, error), (0, ""))
    self.assertEqual(json.loads(output)["message"]["message_id"], 123)
    client.send_message.assert_awaited_once_with(entity=123, message="  exact\r\n**text**\n", reply_to=None)
    client.send_read_acknowledge.assert_awaited_once_with(123)
    client.disconnect.assert_awaited_once()

  def test_ack_failure_exposes_unknown_send_without_retry(self):
    client = self.fake_client(ack_failure=True)
    with patch.object(b, "resolve_api_credentials", return_value=(123, "fake")), patch.object(b, "connect_client", AsyncMock(return_value=(client, None))):
      status, _, error = self.invoke(self.args("send", "--chat", "123", "--message", "a"))
    self.assertEqual(status, 1)
    self.assertEqual(json.loads(error)["error"]["code"], "E_TELEGRAM_USER_SEND_READ_ACK_UNKNOWN")
    client.send_message.assert_awaited_once()
    client.disconnect.assert_awaited_once()

  def test_default_xdg_and_suffix(self):
    with patch.dict(os.environ, {"XDG_DATA_HOME": self.directory.name, "TELEGRAM_USER_SESSION": ""}):
      self.assertEqual(b.resolve_session_path(None), (Path(self.directory.name) / "telegram-user/user.session").resolve())
    self.assertEqual(b.resolve_session_path(str(Path(self.directory.name) / "other")), (Path(self.directory.name) / "other.session").resolve())

  def test_locks_follow_resolved_session_even_with_override(self):
    selected = Path(self.session)
    other = Path(self.directory.name) / "custom.lock"
    locks = b.resolve_session_lock_paths(selected, other)
    self.assertEqual(set(locks), {Path(self.session + ".lock").resolve(), other.resolve()})
    with patch.object(b, "fcntl", None), patch.object(b, "msvcrt", None):
      with self.assertRaises(RuntimeError), b.acquire_session_lock(selected):
        self.fail("unlocked entry")

  def test_pending_login_private_and_clear(self):
    session = Path(self.session)
    b.write_pending_auth_state(session, phone="+123", phone_code_hash="fake", state="awaiting_code")
    path = b.resolve_pending_auth_path(session)
    self.assertEqual(path.name, "user.session.login.json")
    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
    self.assertEqual(b.read_pending_auth_state(session)["state"], "awaiting_code")
    b.clear_pending_auth_state(session)
    self.assertFalse(path.exists())

  def test_secret_redaction(self):
    with patch.dict(os.environ, {"TELEGRAM_API_HASH": "sensitive", b.LOGIN_PASSWORD_ENV: "password"}):
      self.assertEqual(b.sanitize_error_text("sensitive\npassword"), "<redacted> <redacted>")

  def test_parser_unknown_is_json_error(self):
    with patch.object(b.sys, "argv", ["telegram-user", "unknown"]):
      status, _, error = self.invoke(None)
    self.assertEqual(status, 1)
    self.assertEqual(json.loads(error)["error"]["code"], "E_USAGE")

  def test_no_owner_command(self):
    with self.assertRaises(ValueError):
      self.args("owner-claim")

  def test_zero_optional_id_rejected_by_parser(self):
    for command, flag in (("send", "--reply-to"), ("read", "--after-id"), ("read", "--before-id")):
      with self.subTest(command=command, flag=flag), self.assertRaises(ValueError):
        self.args(command, "--chat", "123", flag, "0")

  def test_read_preserves_exact_text_without_ack(self):
    message = SimpleNamespace(id=123, chat_id=123, message="  exact\n ")
    client = SimpleNamespace(get_messages=AsyncMock(return_value=[message]), send_read_acknowledge=AsyncMock(), disconnect=AsyncMock())
    with patch.object(b, "resolve_api_credentials", return_value=(123, "fake")), patch.object(b, "connect_client", AsyncMock(return_value=(client, None))):
      status, output, error = self.invoke(self.args("read", "--chat", "123", "--limit", "1"))
    self.assertEqual((status, error), (0, ""))
    self.assertEqual(json.loads(output)["messages"][0]["text"], "  exact\n ")
    client.send_read_acknowledge.assert_not_awaited()

  def test_send_chat_metadata_failure_is_unknown_with_accepted_id(self):
    client = self.fake_client()
    client.send_message.return_value.get_chat.side_effect = RuntimeError("lookup failed")
    with patch.object(b, "resolve_api_credentials", return_value=(123, "fake")), patch.object(b, "connect_client", AsyncMock(return_value=(client, None))):
      status, output, error = self.invoke(self.args("send", "--chat", "123", "--message", "a"))
    self.assertEqual((status, output), (1, ""))
    self.assertEqual(json.loads(error)["error"]["details"]["message_id"], 123)
    client.send_message.assert_awaited_once()

  def test_login_reuses_pending_code_and_clears_after_signin(self):
    client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(side_effect=[False, False, True]), send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="fake-hash")), sign_in=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=123)))
    with patch.object(b, "resolve_api_credentials", return_value=(123, "fake")), patch.object(b, "create_telegram_client", return_value=client) as create:
      first = self.invoke(self.args("login", "--phone", "+123"))
      with patch.object(b.sys, "stdin", io.StringIO("12345\n")):
        second = self.invoke(self.args("login", "--phone", "+123", "--secret-stdin", "code"))
    self.assertEqual(json.loads(first[1])["state"], "awaiting_code")
    self.assertEqual(json.loads(second[1])["state"], "ready")
    client.send_code_request.assert_awaited_once_with("+123")
    client.sign_in.assert_awaited_once_with(phone="+123", code="12345", phone_code_hash="fake-hash")
    self.assertFalse(b.resolve_pending_auth_path(Path(self.session)).exists())
    self.assertTrue(all(call.kwargs["request_retries"] == 0 for call in create.call_args_list))
    self.assertTrue(all("persistent" not in call.kwargs or call.kwargs["persistent"] for call in create.call_args_list))

  def test_button_mismatch_does_not_click(self):
    button = SimpleNamespace(text="Generic", data=b"generic", url=None)
    message = SimpleNamespace(id=123, buttons=[[button]], click=AsyncMock())
    client = SimpleNamespace(get_messages=AsyncMock(return_value=message), disconnect=AsyncMock())
    with patch.object(b, "resolve_api_credentials", return_value=(123, "fake")), patch.object(b, "connect_client", AsyncMock(return_value=(client, None))):
      status, _, error = self.invoke(self.args("button-click", "--chat", "123", "--message-id", "123", "--button-text", "Generic", "--expected-callback-data", "wrong"))
    self.assertEqual(status, 1)
    self.assertEqual(json.loads(error)["error"]["code"], "E_BUTTON_MISMATCH")
    message.click.assert_not_awaited()


if __name__ == "__main__":
  unittest.main()
