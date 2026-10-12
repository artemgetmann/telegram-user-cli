"""SQLite and fake-Telethon persistence checks without contacting Telegram."""
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telegram_user_cli import compat as c


class CompatTests(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.addCleanup(self.directory.cleanup)
    self.path = Path(self.directory.name) / "user.session"

  def database(self):
    with sqlite3.connect(self.path) as connection:
      connection.execute("CREATE TABLE sessions (dc_id integer primary key, server_address text, port integer, auth_key blob, takeout_id integer)")
      connection.execute("INSERT INTO sessions VALUES (1, 'generic', 443, ?, NULL)", (b"fake-key",))
      connection.execute("CREATE TABLE version (version integer primary key)")
      # This fixture deliberately models the known v8/five-column mismatch.
      connection.execute("INSERT INTO version VALUES (8)")

  def fake_telethon(self):
    class Client:
      def __init__(self, path, api_id, api_hash, **kwargs):
        self.path = path
      async def disconnect(self):
        return None
      def sign_in(self):
        # Model Telethon persisting authentication in its selected database.
        with sqlite3.connect(self.path) as connection:
          connection.execute("UPDATE sessions SET auth_key=?", (b"new-key",))
    return SimpleNamespace(TelegramClient=Client, sessions=SimpleNamespace(sqlite=SimpleNamespace(CURRENT_VERSION=8)))

  def test_persistent_schema_conversion_preserves_and_updates_canonical(self):
    self.database()
    with patch.dict(sys.modules, {"telethon": self.fake_telethon()}):
      client = c.create_telegram_client(self.path, 123, "fake")
      self.assertEqual(client.path, str(self.path))
      client.sign_in()
      asyncio.run(client.disconnect())
    self.assertEqual(c._read_session_columns(self.path), c.SUPPORTED_SESSION_COLUMNS)
    with sqlite3.connect(self.path) as connection:
      self.assertEqual(connection.execute("SELECT auth_key FROM sessions").fetchone()[0], b"new-key")
    self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

  def test_readonly_copy_preserves_original(self):
    self.database()
    before = self.path.read_bytes()
    with patch.dict(sys.modules, {"telethon": self.fake_telethon()}):
      client = c.create_telegram_client(self.path, 123, "fake", persistent=False)
      copied = Path(client.path)
      self.assertNotEqual(copied, self.path)
      client.sign_in()
      asyncio.run(client.disconnect())
    self.assertFalse(copied.exists())
    self.assertEqual(self.path.read_bytes(), before)

  def test_new_session_uses_canonical_path(self):
    with patch.dict(sys.modules, {"telethon": self.fake_telethon()}):
      client = c.create_telegram_client(self.path, 123, "fake")
    self.assertEqual(client.path, str(self.path))
    self.assertFalse(hasattr(client, "_telegram_user_compat_session_path"))

  def test_unknown_schema_not_rewritten(self):
    with sqlite3.connect(self.path) as connection:
      connection.execute("CREATE TABLE sessions (unknown text)")
      connection.execute("CREATE TABLE version (version integer primary key)")
      connection.execute("INSERT INTO version VALUES (8)")
    with patch.dict(sys.modules, {"telethon": self.fake_telethon()}), self.assertRaises(ValueError):
      c.create_telegram_client(self.path, 123, "fake")
    self.assertEqual(c._read_session_columns(self.path), ("unknown",))

  def test_actual_telethon_migrates_genuine_v7_and_keeps_auth_key(self):
    from telethon.sessions import SQLiteSession
    from telethon.crypto import AuthKey
    session = SQLiteSession(str(self.path))
    session.set_dc(1, "149.154.167.50", 443)
    key = bytes(range(256))
    session.auth_key = AuthKey(key)
    session.close()
    with sqlite3.connect(self.path) as connection:
      connection.execute("ALTER TABLE sessions DROP COLUMN tmp_auth_key")
      connection.execute("UPDATE version SET version=7")

    async def construct():
      # Constructing the real client/session is entirely offline.
      client = c.create_telegram_client(self.path, 123, "fake")
      actual_key = client.session.auth_key.key
      await client.disconnect()
      client.session.close()
      return actual_key

    self.assertEqual(asyncio.run(construct()), key)
    self.assertEqual(c._read_session_columns(self.path), c.SUPPORTED_SESSION_COLUMNS)
    with sqlite3.connect(self.path) as connection:
      self.assertEqual(connection.execute("SELECT version FROM version").fetchone()[0], 8)

  def test_future_session_version_rejected_without_changes(self):
    self.database()
    with sqlite3.connect(self.path) as connection:
      connection.execute("UPDATE version SET version=9")
    before = self.path.read_bytes()
    with patch.dict(sys.modules, {"telethon": self.fake_telethon()}), self.assertRaises(ValueError):
      c.create_telegram_client(self.path, 123, "fake")
    self.assertEqual(self.path.read_bytes(), before)

  def test_known_v7_six_columns_reconciles_version(self):
    self.database()
    with sqlite3.connect(self.path) as connection:
      connection.execute("ALTER TABLE sessions ADD COLUMN tmp_auth_key blob")
      connection.execute("UPDATE version SET version=7")
    with patch.dict(sys.modules, {"telethon": self.fake_telethon()}):
      c.create_telegram_client(self.path, 123, "fake")
    with sqlite3.connect(self.path) as connection:
      self.assertEqual(connection.execute("SELECT version FROM version").fetchone()[0], 8)


if __name__ == "__main__":
  unittest.main()
