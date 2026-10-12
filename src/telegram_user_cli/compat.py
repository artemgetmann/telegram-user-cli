#!/usr/bin/env python3
"""
Telethon session compatibility helpers.

Derived from OpenClaw telegram-e2e tooling under the MIT license.

Telethon session schemas drifted over time: some builds use a five-column
`sessions` table, newer ones add `tmp_auth_key`, and real user sessions can be
copied across environments. Read-only callers may normalize a temporary copy;
persistent clients migrate the canonical SQLite table in one transaction while
the backend holds its per-session lock. Login must use the persistent path.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any


LEGACY_SESSION_COLUMNS = (
  "dc_id",
  "server_address",
  "port",
  "auth_key",
  "takeout_id",
)
SUPPORTED_SESSION_COLUMNS = LEGACY_SESSION_COLUMNS + (
  "tmp_auth_key",
)


def _read_session_columns(session_path: Path) -> tuple[str, ...] | None:
  if not session_path.exists() or not session_path.is_file():
    return None

  try:
    connection = sqlite3.connect(f"file:{session_path}?mode=ro", uri = True)
  except sqlite3.Error:
    return None

  try:
    rows = connection.execute("PRAGMA table_info(sessions)").fetchall()
  except sqlite3.Error:
    return None
  finally:
    connection.close()

  if not rows:
    return None
  return tuple(str(row[1]) for row in rows)


def _copy_session_database(source_path: Path, destination_path: Path) -> None:
  destination_path.parent.mkdir(parents = True, exist_ok = True)
  source = sqlite3.connect(f"file:{source_path}?mode=ro", uri = True)
  destination = sqlite3.connect(destination_path)
  try:
    # SQLite backup folds in committed WAL state without opening the original
    # session for writes or relying on sidecar file copies.
    source.backup(destination)
  finally:
    destination.close()
    source.close()


def _repair_session_schema(session_path: Path, *, target_version: int) -> None:
  if not session_path.exists() or not session_path.is_file():
    return
  connection = sqlite3.connect(session_path)
  try:
    row = connection.execute("SELECT version FROM version").fetchone()
    if row is None or not isinstance(row[0], int):
      raise ValueError("Unsupported Telegram session version metadata.")
    version = row[0]
    if version > target_version:
      raise ValueError("Telegram session was created by a newer unsupported Telethon version.")
    columns = tuple(str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)"))
    # Genuine historical versions belong to Telethon's native migration path.
    # In particular, adding tmp_auth_key to v7 before its own v8 upgrade would
    # cause duplicate-column errors. Only repair the two known mixed states.
    if version == 7 and columns == SUPPORTED_SESSION_COLUMNS and target_version == 8:
      with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE version SET version=8")
    elif version == 8 and columns == LEGACY_SESSION_COLUMNS and target_version == 8:
      with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE sessions ADD COLUMN tmp_auth_key blob")
    elif version >= 7 and columns != (SUPPORTED_SESSION_COLUMNS if version == 8 else LEGACY_SESSION_COLUMNS):
      raise ValueError("Unsupported Telegram session schema; refusing to rewrite it.")
  except sqlite3.Error as err:
    raise ValueError("Unsupported Telegram session database metadata.") from err
  finally:
    connection.close()


def detect_expected_session_columns(telethon: Any) -> tuple[str, ...]:
  current_version = int(getattr(telethon.sessions.sqlite, "CURRENT_VERSION", 0) or 0)
  if current_version >= 8:
    return SUPPORTED_SESSION_COLUMNS
  return LEGACY_SESSION_COLUMNS


def prepare_session_for_telethon(
  session_path: str | Path,
  *,
  target_columns: tuple[str, ...],
) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
  normalized_path = Path(session_path).expanduser()
  if not normalized_path.exists() or not normalized_path.is_file():
    return normalized_path, None

  temp_dir = tempfile.TemporaryDirectory(
    dir = normalized_path.parent,
    prefix = f".{normalized_path.stem}.telethon-compat-",
  )
  compat_path = Path(temp_dir.name) / normalized_path.name
  try:
    _copy_session_database(normalized_path, compat_path)
    _repair_session_schema(compat_path, target_version=8 if target_columns == SUPPORTED_SESSION_COLUMNS else 7)
  except Exception:
    temp_dir.cleanup()
    raise
  return compat_path, temp_dir


def create_telegram_client(
  session_path: str | Path,
  api_id: int,
  api_hash: str,
  *,
  persistent: bool = True,
  **kwargs: Any,
):
  try:
    import telethon  # type: ignore
  except Exception as err:  # pragma: no cover - passthrough for missing package
    raise RuntimeError(f"Telethon import failed: {err}") from err

  normalized = Path(session_path).expanduser()
  target_columns = detect_expected_session_columns(telethon)
  if persistent:
    # Login retains keys in the canonical file. Known genuine old versions
    # migrate in Telethon; narrowly repair inconsistent version/schema pairs.
    normalized.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target_version = int(telethon.sessions.sqlite.CURRENT_VERSION)
    if target_version not in (7, 8):
      raise ValueError("Unsupported Telethon session version.")
    _repair_session_schema(normalized, target_version=target_version)
    prepared_path, temp_dir = normalized, None
  else:
    prepared_path, temp_dir = prepare_session_for_telethon(normalized, target_columns=target_columns)
  try:
    client = telethon.TelegramClient(str(prepared_path), api_id, api_hash, **kwargs)
  except Exception:
    if temp_dir is not None:
      temp_dir.cleanup()
    raise

  if persistent and prepared_path.exists():
    # Telethon owns creation; tighten the database immediately after its SQLite
    # session constructor creates it, independently of the caller's umask.
    import os
    os.chmod(prepared_path, 0o600)
  if temp_dir is None:
    return client

  original_disconnect = client.disconnect

  async def disconnect_with_cleanup(*args: Any, **inner_kwargs: Any):
    try:
      return await original_disconnect(*args, **inner_kwargs)
    finally:
      temp_dir.cleanup()

  client.disconnect = disconnect_with_cleanup
  client._telegram_user_compat_session_path = str(prepared_path)
  return client
