#!/usr/bin/env python3
"""
Standalone Telegram user transport.

Derived from OpenClaw telegram-e2e tooling, used under the MIT license.

The CLI owns operator UX. This backend connects safely, sends exact text,
and reads normalized message
metadata without leaking secrets into stderr/stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime
import json
import io
import os
import re
import select
import stat
import sys
import time
from contextlib import ExitStack, contextmanager, asynccontextmanager
from pathlib import Path
from typing import Any


try:
  import fcntl
except Exception:  # pragma: no cover - Windows fallback
  fcntl = None

try:
  import msvcrt
except Exception:  # pragma: no cover - POSIX fallback
  msvcrt = None

from .compat import create_telegram_client

try:
  from telethon import functions, utils
except Exception:  # pragma: no cover - import failure is reported by command execution
  functions = None
  utils = None


# Resolve the default at invocation time so changing XDG_DATA_HOME before a
# command is honored without import-time filesystem access.
DEFAULT_SESSION = Path.home() / ".local" / "share" / "telegram-user" / "user.session"
DEFAULT_LOCK_TIMEOUT_SECONDS = 15
PENDING_AUTH_SUFFIX = ".login.json"
LOGIN_PASSWORD_ENV = "TELEGRAM_USER_LOGIN_PASSWORD"
SESSION_LOCK_PATH_ENV = "TELEGRAM_USER_LOCK_PATH"
MAX_INPUT_BYTES = 4 * 1024 * 1024
# Validate the original button value with one ASCII-only grammar. Component URL
# parsers may erase empty `?`/`#` delimiters, trim leading controls, or normalize
# host casing before callers can decide whether the provider URL was canonical.
TELEGRAM_JOIN_URL_PATTERN = re.compile(
  r"https://(?:t\.me|telegram\.me)(?:"
  r"/\+(?P<plus_invite>[A-Za-z0-9_-]{5,})"
  r"|/joinchat/(?P<joinchat_invite>[A-Za-z0-9_-]{5,})"
  r"|/(?P<public_username>[A-Za-z][A-Za-z0-9_]{3,31})"
  r")",
  re.ASCII,
)
TELEGRAM_RESERVED_PATHS = {
  "addlist",
  "addstickers",
  "boost",
  "c",
  "confirmphone",
  "contact",
  "giftcode",
  "invoice",
  "iv",
  "joinchat",
  "login",
  "m",
  "nft",
  "proxy",
  "s",
  "setlanguage",
  "share",
  "socks",
}


def emit(payload: object, *, stream = None) -> int:
  stream = sys.stdout if stream is None else stream
  stream.write(json.dumps(payload, ensure_ascii=True))
  stream.write("\n")
  stream.flush()
  return 0


def sanitize_error_text(raw: str) -> str:
  text = raw.replace("\n", " ").replace("\r", " ").strip()
  for secret in (
    os.environ.get("TELEGRAM_API_HASH", ""),
    os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    os.environ.get("TG_BOT_TOKEN", ""),
    os.environ.get(LOGIN_PASSWORD_ENV, ""),
  ):
    if secret:
      text = text.replace(secret, "<redacted>")
  text = re.sub(r"\s+", " ", text).strip()
  return text[:400] if text else "unexpected error"


def fail(code: str, message: str, *, details: dict[str, object] | None = None, exit_code: int = 1) -> int:
  emit(
    {
      "error": {
        "code": code,
        "message": message,
        "details": details or None,
      }
    },
    stream = sys.stderr,
  )
  return exit_code


def resolve_api_credentials() -> tuple[int, str]:
  api_id_raw = (os.environ.get("TELEGRAM_API_ID") or "").strip()
  api_hash = (os.environ.get("TELEGRAM_API_HASH") or "").strip()
  if not api_id_raw or not api_hash:
    raise ValueError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required.")
  if not api_id_raw.isdigit() or api_id_raw == "0":
    raise ValueError("TELEGRAM_API_ID must be a positive integer.")
  return int(api_id_raw), api_hash


def resolve_session_path(explicit: str | None) -> Path:
  from_env = (os.environ.get("TELEGRAM_USER_SESSION") or "").strip()
  data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share").expanduser()
  raw = explicit or from_env or str(data_home / "telegram-user" / "user.session")
  # Telethon appends .session itself; normalize first so database, pending auth
  # and lock selectors always refer to the same exact persisted path.
  path = Path(raw).expanduser()
  if path.suffix != ".session":
    path = Path(str(path) + ".session")
  return path.resolve()


def resolve_chat(chat_raw: str) -> int | str:
  chat = chat_raw.strip()
  if chat.lstrip("-").isdigit():
    return int(chat)
  return chat


def resolve_pending_auth_path(session_path: Path) -> Path:
  return session_path.with_name(f"{session_path.name}{PENDING_AUTH_SUFFIX}")


def is_valid_pending_auth_state(state: str) -> bool:
  return state in {"awaiting_code", "awaiting_password"}


def read_pending_auth_state(session_path: Path) -> dict[str, object] | None:
  pending_path = resolve_pending_auth_path(session_path)
  if not pending_path.exists() or not pending_path.is_file():
    return None
  try:
    payload = json.loads(pending_path.read_text(encoding = "utf-8"))
  except Exception:
    return None
  if not isinstance(payload, dict):
    return None
  return payload


def write_pending_auth_state(
  session_path: Path,
  *,
  phone: str,
  phone_code_hash: str,
  state: str,
) -> dict[str, object]:
  payload = {
    "phone": phone,
    "phone_code_hash": phone_code_hash,
    "state": state,
  }
  pending_path = resolve_pending_auth_path(session_path)
  pending_path.parent.mkdir(parents = True, exist_ok = True)
  # The OTP hash is secret. Create private state even when the process umask is
  # permissive; session locks serialize readers and writers of this file.
  descriptor = os.open(pending_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  os.chmod(pending_path, 0o600)
  with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
  return payload


def clear_pending_auth_state(session_path: Path) -> None:
  pending_path = resolve_pending_auth_path(session_path)
  try:
    pending_path.unlink()
  except FileNotFoundError:
    return


def build_user_payload(me) -> dict[str, object | None]:
  return {
    "first_name": getattr(me, "first_name", None),
    "user_id": int(getattr(me, "id", 0) or 0),
    "username": getattr(me, "username", None),
  }


def build_pending_login_payload(pending_auth: dict[str, object] | None) -> dict[str, object | None] | None:
  if not pending_auth:
    return None
  state = str(pending_auth.get("state") or "").strip()
  phone = str(pending_auth.get("phone") or "").strip()
  if not is_valid_pending_auth_state(state):
    return None
  return {
    "phone": phone or None,
    "state": state,
  }


def emit_auth_status(
  *,
  chat_payload: dict[str, object | None] | None,
  pending_auth: dict[str, object] | None,
  session_path: Path,
  state: str,
  user_payload: dict[str, object | None] | None,
  auth_error: str | None = None,
  retry_after_seconds: int | None = None,
) -> int:
  return emit(
    {
      "chat": chat_payload,
      "pending_login": build_pending_login_payload(pending_auth),
      "session_path": str(session_path),
      "state": state,
      "user": user_payload,
      **({"auth_error": auth_error} if auth_error else {}),
      **({"retry_after_seconds": retry_after_seconds} if retry_after_seconds is not None else {}),
    }
  )


def clear_session_artifacts(session_path: Path) -> list[str]:
  # Logout removes only the explicitly selected database and its known sidecars.
  paths = [session_path, resolve_pending_auth_path(session_path), *[Path(str(session_path)+suffix) for suffix in ("-journal", "-shm", "-wal")]]
  if any(path.is_dir() for path in paths):
    raise ValueError("Refusing to delete a directory session artifact.")
  removed = []
  for path in paths:
    try:
      path.unlink()
      removed.append(str(path))
    except FileNotFoundError:
      pass
  return removed


async def refresh_pending_code_request(
  client,
  session_path: Path,
  *,
  phone: str,
) -> dict[str, object]:
  sent = await client.send_code_request(phone)
  return write_pending_auth_state(
    session_path,
    phone = phone,
    phone_code_hash = str(getattr(sent, "phone_code_hash", "") or ""),
    state = "awaiting_code",
  )


def classify_login_error(error: Exception) -> str:
  return error.__class__.__name__


def resolve_session_lock_path(explicit: str | Path | None = None, *, session_path: Path | None = None) -> Path:
  raw = explicit or (os.environ.get(SESSION_LOCK_PATH_ENV) or "").strip()
  if raw:
    selected = Path(raw).expanduser()
    if not selected.is_absolute():
      raise ValueError("Telegram user lock override must be absolute.")
    return selected.resolve()
  resolved = (session_path or resolve_session_path(None)).resolve()
  return Path(str(resolved) + ".lock")


def resolve_session_lock_paths(session_path: Path, lock_path_override: str | Path | None = None) -> list[Path]:
  # Always retain the per-session lock, even with an override. An override must
  # not allow two clients of the same SQLite session to run concurrently.
  paths = {Path(str(session_path.resolve()) + ".lock"), resolve_session_lock_path(lock_path_override, session_path=session_path)}
  return sorted(paths, key=str)


@contextmanager
def acquire_session_locks(session_paths: list[Path], timeout_seconds: int = DEFAULT_LOCK_TIMEOUT_SECONDS, *, lock_path_override=None):
  if fcntl is None and msvcrt is None:
    raise RuntimeError("Session locking is unavailable on this operating system.")
  paths = sorted({p for session in session_paths for p in resolve_session_lock_paths(session, lock_path_override)}, key=str)
  deadline = time.monotonic() + max(0, timeout_seconds)
  acquired = []
  with ExitStack() as stack:
    try:
      for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = stack.enter_context(open(path, "a+", encoding="utf-8"))
        os.chmod(path, 0o600)
        if msvcrt is not None and fcntl is None:
          # Windows byte-range locking requires a byte and a stable offset.
          handle.seek(0)
          if not handle.read(1):
            handle.write(" ")
            handle.flush()
          handle.seek(0)
        while True:
          try:
            if fcntl is not None:
              fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
              handle.seek(0)
              msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            acquired.append(handle)
            break
          except OSError:
            if time.monotonic() >= deadline:
              raise TimeoutError("timed out waiting for Telegram user session lock") from None
            time.sleep(0.2)
      yield
    finally:
      for handle in reversed(acquired):
        if fcntl is not None:
          fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:
          handle.seek(0)
          msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def acquire_session_lock(session_path: Path, timeout_seconds: int = DEFAULT_LOCK_TIMEOUT_SECONDS, *, lock_path_override=None):
  with acquire_session_locks([session_path], timeout_seconds, lock_path_override=lock_path_override):
    yield


@asynccontextmanager
async def acquire_session_lock_async(session_path: Path, timeout_seconds=DEFAULT_LOCK_TIMEOUT_SECONDS, *, lock_path_override=None):
  # Runtime locking must yield while another process owns SQLite. This keeps
  # the outer RPC deadline and cancellation effective without worker threads.
  if fcntl is None and msvcrt is None:
    raise RuntimeError("Session locking is unavailable on this operating system.")
  deadline = time.monotonic() + max(0, timeout_seconds)
  acquired = []
  with ExitStack() as stack:
    try:
      for path in resolve_session_lock_paths(session_path, lock_path_override):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = stack.enter_context(open(path, "a+", encoding="utf-8"))
        os.chmod(path, 0o600)
        if fcntl is None:
          handle.seek(0)
          if not handle.read(1):
            handle.write(" ")
            handle.flush()
        while True:
          try:
            if fcntl is not None:
              fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
              handle.seek(0)
              msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            acquired.append(handle)
            break
          except OSError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
              raise TimeoutError("timed out waiting for Telegram user session lock") from None
            await asyncio.sleep(min(0.05, remaining))
      yield
    finally:
      for handle in reversed(acquired):
        if fcntl is not None:
          fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:
          handle.seek(0)
          msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def canonical_peer_id(entity):
  # Marked Telegram IDs distinguish users (+id), basic groups (-id), and
  # channels/supergroups (-100…id). Bare entity.id can select another recipient.
  if entity is None:
    return None
  if utils is None:
    raise RuntimeError("Telethon peer utilities are unavailable.")
  return utils.get_peer_id(entity)


def build_chat_payload(chat) -> dict[str, object | None]:
  return {
    "chat_id": canonical_peer_id(chat),
    "peer_type": type(chat).__name__,
    "title": getattr(chat, "title", None),
    "username": getattr(chat, "username", None),
  }


def build_message_payload(message, *, chat = None) -> dict[str, object | None]:
  reply_to = getattr(message, "reply_to", None)
  direct_topic_id = getattr(getattr(message, "direct_messages_topic", None), "topic_id", None)
  thread_anchor = (
    direct_topic_id
    if direct_topic_id is not None
    else getattr(reply_to, "reply_to_top_id", None)
    if reply_to is not None and getattr(reply_to, "reply_to_top_id", None) is not None
    else getattr(reply_to, "reply_to_msg_id", None)
    if reply_to is not None
    else None
  )
  resolved_chat = chat if chat is not None else getattr(message, "chat", None)
  media_kind = None
  # Keep readback lightweight, but expose enough media shape for live proof gates
  # to distinguish a captionless voice/audio supplement from an empty text reply.
  if getattr(message, "voice", None) is not None:
    media_kind = "voice"
  elif getattr(message, "audio", None) is not None:
    media_kind = "audio"
  elif getattr(message, "photo", None) is not None:
    media_kind = "photo"
  elif getattr(message, "video", None) is not None:
    media_kind = "video"
  elif getattr(message, "document", None) is not None:
    media_kind = "document"
  inline_buttons = [metadata for _, metadata in list_message_buttons(message)[:100]]
  return {
    "chat_id": int(getattr(message, "chat_id", 0) or 0) or None,
    "chat_title": getattr(resolved_chat, "title", None),
    "chat_username": getattr(resolved_chat, "username", None),
    "date": getattr(message, "date", None).isoformat() if getattr(message, "date", None) else None,
    "direct_messages_topic": {"topic_id": int(direct_topic_id)} if direct_topic_id is not None else None,
    "direct_messages_topic_id": int(direct_topic_id) if direct_topic_id is not None else None,
    "inline_buttons": inline_buttons,
    "message_id": int(getattr(message, "id", 0) or 0),
    "media_kind": media_kind,
    "out": bool(getattr(message, "out", False)),
    "reply_to_msg_id": int(getattr(reply_to, "reply_to_msg_id", 0)) if getattr(reply_to, "reply_to_msg_id", None) is not None else None,
    "reply_to_top_id": int(getattr(reply_to, "reply_to_top_id", 0)) if getattr(reply_to, "reply_to_top_id", None) is not None else None,
    "sender_id": int(getattr(message, "sender_id", 0) or 0) or None,
    "text": (getattr(message, "message", "") or ""),
    "thread_anchor": int(thread_anchor) if thread_anchor is not None else None,
  }


def coerce_single_message(sent):
  # Telethon returns a single Message for normal uploads, but albums can return
  # a list. This CLI intentionally sends one media item at a time, so normalize
  # the occasional list result without widening the command contract.
  if isinstance(sent, list):
    return sent[0] if sent else None
  return sent


def build_topic_create_payload(
  *,
  chat_raw: str,
  message,
  title: str,
) -> dict[str, object | None]:
  chat_id = getattr(message, "chat_id", None)
  if chat_id is None:
    resolved_chat = resolve_chat(chat_raw)
    chat_id = resolved_chat if isinstance(resolved_chat, int) else None
  anchor = int(getattr(message, "id", 0) or 0)
  if anchor <= 0:
    raise RuntimeError(f"no topic anchor for {title}")
  return {
    "chat_id": int(chat_id) if chat_id is not None else None,
    "message_id": anchor,
    "topic_anchor": anchor,
    "topic_title": title,
  }


def build_topic_delete_payload(
  *,
  chat_raw: str,
  result,
  topic_anchor: int,
) -> dict[str, object | None]:
  resolved_chat = resolve_chat(chat_raw)
  # DeleteTopicHistoryRequest returns an AffectedHistory object. Keep only the
  # stable counters that help prove Telegram accepted the bounded cleanup.
  return {
    "affected": {
      "offset": int(getattr(result, "offset", 0) or 0),
      "pts": int(getattr(result, "pts", 0) or 0),
      "pts_count": int(getattr(result, "pts_count", 0) or 0),
    },
    "chat_id": resolved_chat if isinstance(resolved_chat, int) else None,
    "deleted": True,
    "topic_anchor": topic_anchor,
  }


def build_topic_payload(topic) -> dict[str, object]:
  # ForumTopic.id is Telegram's canonical topic anchor. It is 1 for General
  # and the creation-service-message id for normal supergroup topics.
  return {
    "closed": bool(getattr(topic, "closed", False)),
    "hidden": bool(getattr(topic, "hidden", False)),
    "topic_anchor": int(getattr(topic, "id", 0) or 0),
    "topic_title": str(getattr(topic, "title", "") or ""),
  }


def find_topic_by_anchor(result, topic_anchor: int):
  for topic in getattr(result, "topics", []) or []:
    if int(getattr(topic, "id", 0) or 0) == topic_anchor and getattr(topic, "title", None) is not None:
      return topic
  return None


async def fetch_topic_by_anchor(client, *, chat, topic_anchor: int):
  # This RPC binds the topic id to the selected peer. An empty/deleted result
  # therefore fails closed instead of letting a same-numbered topic elsewhere
  # authorize a read.
  result = await client(
    functions.messages.GetForumTopicsByIDRequest(
      peer = chat,
      topics = [topic_anchor],
    )
  )
  return find_topic_by_anchor(result, topic_anchor)


def extract_created_topic_message(updates):
  # Forum topic creation arrives as an Updates container with a service message.
  # Match the structural Telegram action class so localized text never affects
  # topic-anchor extraction.
  for update in getattr(updates, "updates", []) or []:
    message = getattr(update, "message", None)
    action = getattr(message, "action", None)
    if action and action.__class__.__name__ == "MessageActionTopicCreate":
      return message
  return None


def dialog_has_unread(dialog) -> bool:
  return any(
    int(getattr(dialog, field, 0) or 0) > 0
    for field in ("unread_count", "unread_mentions_count", "unread_reactions_count")
  )


def text_contains(value: object, needle: str) -> bool:
  return bool(needle) and needle in str(value or "")


def message_matches_text(message, *, contains: str) -> bool:
  if not contains:
    return True
  return text_contains(getattr(message, "message", ""), contains)


def dialog_matches_text(dialog, *, contains: str) -> bool:
  if not contains:
    return True
  # Match the fields agents usually grep from raw inbox JSON: chat labels and
  # preview text. This stays a structured operator selector, not full-history
  # Telegram search.
  entity = getattr(dialog, "entity", None)
  last_message = getattr(dialog, "message", None)
  candidates = (
    getattr(dialog, "name", ""),
    getattr(entity, "title", ""),
    getattr(entity, "username", ""),
    getattr(last_message, "message", ""),
  )
  return any(text_contains(candidate, contains) for candidate in candidates)


def dialog_matches_inbox_filters(dialog, *, contains: str = "", dm_only: bool, unread_only: bool) -> bool:
  if dm_only and not bool(getattr(dialog, "is_user", False)):
    return False
  if unread_only and not dialog_has_unread(dialog):
    return False
  if not dialog_matches_text(dialog, contains = contains):
    return False
  return True


def compute_inbox_scan_cap(*, contains: str = "", limit: int, dm_only: bool, unread_only: bool) -> int:
  # Unfiltered inbox reads can stop at the caller's requested window because every
  # scanned dialog is eligible for return. Filtered reads are different: a busy
  # account can have hundreds of pinned/groups/read chats ahead of the first
  # unread DM, so we reserve a larger but still bounded scan budget.
  if not contains and not dm_only and not unread_only:
    return limit

  filter_multiplier = 12
  if contains:
    filter_multiplier = 50
  if dm_only and unread_only:
    filter_multiplier = max(filter_multiplier, 25)
  elif dm_only or unread_only:
    filter_multiplier = max(filter_multiplier, 15)
  return min(5_000, max(limit * filter_multiplier, 1_000))


def compute_message_scan_cap(*, contains: str = "", limit: int) -> int:
  if not contains:
    return limit
  # With text filtering, limit means "matches returned", not the shallow
  # collection window. Scan deeper so operators do not need `--limit 200 | grep`.
  return min(1_000, max(limit * 50, 200))


def build_dialog_payload(dialog) -> dict[str, object | None]:
  entity = getattr(dialog, "entity", None)
  notify_settings = getattr(dialog, "dialog", None)
  mute_until = getattr(getattr(notify_settings, "notify_settings", None), "mute_until", None)
  # Telethon can surface mute_until either as an epoch-like integer or as a
  # concrete datetime. Normalize both shapes before comparing against "now" so
  # inbox serialization never crashes on unread dialog enumeration.
  if isinstance(mute_until, datetime):
    mute_until_epoch = mute_until.timestamp()
  elif mute_until is None:
    mute_until_epoch = None
  else:
    mute_until_epoch = float(mute_until)
  muted = bool(mute_until_epoch and mute_until_epoch > time.time())
  name = str(getattr(dialog, "name", "") or "").strip()
  title = getattr(entity, "title", None)
  username = getattr(entity, "username", None)
  last_message = getattr(dialog, "message", None)
  return {
    "archived": bool(getattr(dialog, "archived", False)),
    "chat_id": canonical_peer_id(entity),
    "chat_title": title,
    "chat_username": username,
    "display_name": name or title or username or str(getattr(entity, "id", "unknown")),
    "folder_id": int(getattr(dialog, "folder_id", 0)) if getattr(dialog, "folder_id", None) is not None else None,
    "is_bot": bool(getattr(entity, "bot", False)),
    "is_channel": bool(getattr(dialog, "is_channel", False)),
    "is_group": bool(getattr(dialog, "is_group", False)),
    "is_user": bool(getattr(dialog, "is_user", False)),
    "last_message": build_message_payload(last_message, chat = entity) if last_message is not None else None,
    "muted": muted,
    "pinned": bool(getattr(dialog, "pinned", False)),
    "unread_count": int(getattr(dialog, "unread_count", 0) or 0),
    "unread_mentions_count": int(getattr(dialog, "unread_mentions_count", 0) or 0),
    "unread_reactions_count": int(getattr(dialog, "unread_reactions_count", 0) or 0),
  }


def sanitize_filename_component(value: object) -> str:
  text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
  return text.strip("-._") or "chat"


def guess_media_extension(message) -> str:
  file_info = getattr(message, "file", None)
  file_ext = str(getattr(file_info, "ext", "") or "").strip()
  if file_ext.startswith(".") and len(file_ext) > 1:
    return file_ext
  mime_type = str(getattr(file_info, "mime_type", "") or getattr(message, "mime_type", "") or "")
  if "ogg" in mime_type or getattr(message, "voice", None) is not None:
    return ".oga"
  if "mpeg" in mime_type or "mp3" in mime_type:
    return ".mp3"
  if "wav" in mime_type:
    return ".wav"
  if "mp4" in mime_type:
    return ".mp4"
  if getattr(message, "photo", None) is not None:
    return ".jpg"
  return ".bin"


def resolve_download_output_path(output_raw: str, *, chat_raw: str, message) -> Path:
  output = Path(output_raw).expanduser()
  if output.exists() and output.is_dir():
    target_dir = output
  elif not output.exists() and not output.suffix:
    target_dir = output
  else:
    output.parent.mkdir(parents = True, exist_ok = True)
    return output

  target_dir.mkdir(parents = True, exist_ok = True)
  message_id = int(getattr(message, "id", 0) or 0)
  chat_component = sanitize_filename_component(chat_raw)
  return target_dir / f"telegram-{chat_component}-{message_id}{guess_media_extension(message)}"


def build_button_metadata(button, *, column: int, row: int) -> dict[str, object]:
  """Return JSON-safe identity metadata without following the button."""

  text = str(getattr(button, "text", "") or "")
  data = getattr(button, "data", None)
  callback_bytes = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else None
  callback_text = None
  if callback_bytes is not None:
    try:
      callback_text = callback_bytes.decode("utf-8")
    except UnicodeDecodeError:
      callback_text = None
  return {
    "callback_data": callback_text,
    "callback_data_base64": (
      base64.b64encode(callback_bytes).decode("ascii") if callback_bytes is not None else None
    ),
    "column": column,
    "row": row,
    "text": text[:200],
    # URL buttons do not carry callback bytes. Expose the provider value in
    # structured reads so a later action can repeat it as an exact guard.
    "url": str(getattr(button, "url", "") or "") or None,
  }


def list_message_buttons(message) -> list[tuple[object, dict[str, object]]]:
  available = []
  for row_index, row in enumerate(getattr(message, "buttons", None) or []):
    for column_index, button in enumerate(row or []):
      available.append(
        (
          button,
          build_button_metadata(button, column = column_index, row = row_index),
        )
      )
  return available


def build_button_click_result_payload(result) -> dict[str, object | None]:
  """Expose only bounded callback-answer fields, never an arbitrary RPC object."""

  # URL actions reuse this bounded shape for their verified value. The CLI
  # never passes Telethon's open_url=True and therefore never launches a browser.
  if isinstance(result, str):
    return {
      "alert": False,
      "cache_time": 0,
      "message": None,
      "url": result,
    }
  return {
    "alert": bool(getattr(result, "alert", False)),
    "cache_time": int(getattr(result, "cache_time", 0) or 0),
    "message": str(getattr(result, "message", "") or "")[:400] or None,
    "url": str(getattr(result, "url", "") or "")[:400] or None,
  }


def parse_telegram_join_url(url: str) -> tuple[str, str] | None:
  """Recognize only exact Telegram public-chat and private-invite URL forms."""

  # `fullmatch` is intentional: unlike `$`, it cannot accept a trailing newline.
  # Extract components only after the untouched input passes the whole grammar.
  matched = TELEGRAM_JOIN_URL_PATTERN.fullmatch(url)
  if matched is None:
    return None

  invite_hash = matched.group("plus_invite") or matched.group("joinchat_invite")
  if invite_hash is not None:
    return "import_chat_invite", invite_hash
  username = matched.group("public_username")
  if username is None:  # Defensive assertion for future grammar changes.
    return None
  if username.lower() in TELEGRAM_RESERVED_PATHS:
    return None
  return "join_public_chat", username


async def run_telegram_url_action(client, *, url: str) -> dict[str, object]:
  """Join one exact Telegram chat URL without invoking a browser or web client."""

  parsed = parse_telegram_join_url(url)
  if parsed is None:
    raise ValueError("unsupported Telegram join URL")
  kind, value = parsed
  request = (
    functions.messages.ImportChatInviteRequest(hash = value)
    if kind == "import_chat_invite"
    else functions.channels.JoinChannelRequest(channel = value)
  )
  status = "joined"
  try:
    await client(request)
  except Exception as err:
    error_name = err.__class__.__name__
    # These two RPC outcomes are both successful external transitions. One is
    # already complete; the other is explicitly pending an admin decision.
    # Returning either as a stable status prevents a blind retry from creating
    # ambiguous provider state after the original request was accepted.
    if error_name == "UserAlreadyParticipantError":
      status = "already_member"
    elif error_name == "InviteRequestSentError":
      status = "request_sent"
    else:
      raise
  return {
    "kind": kind,
    "status": status,
    "url": url,
  }


class JsonArgumentParser(argparse.ArgumentParser):
  def error(self, message):
    raise ValueError(message)


def positive_int(raw: str) -> int:
  value = int(raw)
  if value <= 0:
    raise argparse.ArgumentTypeError("must be a positive integer")
  return value


def resolve_text_payload(args, name: str) -> str:
  inline = getattr(args, name, None)
  file_path = getattr(args, name + "_file", None)
  stdin = bool(getattr(args, name + "_stdin", False))
  if sum((inline is not None, file_path is not None, stdin)) > 1:
    raise ValueError(f"Choose one of --{name}, --{name}-file, or --{name}-stdin.")
  if inline is not None:
    # Shell escaping cannot prove the author's intended line boundaries.
    if any(value in inline for value in ("\n", "\r", "\t", "\\")):
      raise ValueError(f"Use --{name}-file for multiline or escape-sensitive text.")
    raw = inline.encode("utf-8")
  elif file_path is not None and file_path != "-":
    path = Path(file_path).expanduser()
    if not stat.S_ISREG(path.stat().st_mode):
      raise ValueError("Text input must be a regular UTF-8 file; use - for a pipe.")
    with open(path, "rb") as handle:
      raw = handle.read(MAX_INPUT_BYTES + 1)
  elif stdin or file_path == "-":
    if hasattr(sys.stdin, "buffer"):
      raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
      raw = sys.stdin.read(MAX_INPUT_BYTES + 1).encode("utf-8")
  else:
    return ""
  if len(raw) > MAX_INPUT_BYTES:
    raise ValueError("Text input exceeds 4 MiB.")
  return raw.decode("utf-8")


class InputTimeoutError(TimeoutError):
  pass


async def read_stdin_bounded(args, *, single_line=False) -> bytes:
  # Pipes may never close. Poll a real POSIX descriptor without blocking the
  # event loop or leaving a thread stuck in read() after timeout/cancellation.
  timeout = float(getattr(args, "rpc_timeout", 60))
  deadline = time.monotonic() + timeout
  try:
    fd = sys.stdin.fileno()
  except (AttributeError, io.UnsupportedOperation):
    # In-memory streams are finite and used by interactive-login/test callers.
    raw = sys.stdin.readline(MAX_INPUT_BYTES + 1) if single_line else sys.stdin.read(MAX_INPUT_BYTES + 1)
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(data) > MAX_INPUT_BYTES:
      raise ValueError("Text input exceeds 4 MiB.")
    return data
  if os.name != "posix":
    raise ValueError("Bounded stdin requires POSIX; use a UTF-8 file instead.")
  data = bytearray()
  while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
      raise InputTimeoutError("Timed out waiting for stdin; the producer must close the pipe.")
    if not select.select([fd], [], [], 0)[0]:
      await asyncio.sleep(min(0.02, remaining))
      continue
    chunk = os.read(fd, 1 if single_line else min(65536, MAX_INPUT_BYTES + 1 - len(data)))
    if not chunk:
      return bytes(data)
    data.extend(chunk)
    if len(data) > MAX_INPUT_BYTES:
      raise ValueError("Text input exceeds 4 MiB.")
    if single_line and chunk == b"\n":
      return bytes(data)


async def resolve_text_payload_async(args, name: str) -> str:
  if getattr(args, name + "_stdin", False) or getattr(args, name + "_file", None) == "-":
    return (await read_stdin_bounded(args)).decode("utf-8")
  return resolve_text_payload(args, name)


def validate_args(args):
  # Reject malformed bounds and conflicting selectors before opening a session
  # or contacting Telegram. Zero is only valid for optional absent ID bounds.
  if float(getattr(args, "rpc_timeout", 60)) <= 0:
    raise ValueError("--rpc-timeout must be positive.")
  for name in ("message_id", "topic_anchor", "limit"):
    value = getattr(args, name, None)
    if value is not None and (isinstance(value, bool) or int(value) <= 0):
      raise ValueError(f"--{name.replace('_', '-')} must be positive.")
  for name in ("reply_to", "after_id", "before_id"):
    value = getattr(args, name, 0)
    if value is not None and int(value) < 0:
      raise ValueError(f"--{name.replace('_', '-')} must be positive when supplied.")
  if getattr(args, "limit", 0) > 200:
    raise ValueError("--limit must be at most 200.")
  if getattr(args, "after_id", 0) and getattr(args, "before_id", 0) and args.after_id >= args.before_id:
    raise ValueError("--after-id must be less than --before-id.")
  if getattr(args, "topic_anchor", None) and getattr(args, "reply_to", 0):
    raise ValueError("--topic-anchor and --reply-to are mutually exclusive.")
  if args.command == "send":
    message_sources = sum((getattr(args, "message", None) is not None, getattr(args, "message_file", None) is not None, bool(getattr(args, "message_stdin", False))))
    caption_sources = sum((getattr(args, "caption", None) is not None, getattr(args, "caption_file", None) is not None, bool(getattr(args, "caption_stdin", False))))
    if message_sources > 1 or caption_sources > 1:
      raise ValueError("Choose exactly one source for each text payload.")
    if caption_sources and not getattr(args, "media", None):
      raise ValueError("A caption requires --media.")
    if caption_sources and message_sources:
      raise ValueError("Message and caption text sources are mutually exclusive.")
    if getattr(args, "voice", False) and not getattr(args, "media", None):
      raise ValueError("--voice requires --media.")
  chat = getattr(args, "chat", None)
  if chat is not None and (not str(chat).strip() or str(chat).strip() == "0"):
    raise ValueError("--chat must be a nonzero ID or username.")


def build_parser() -> argparse.ArgumentParser:
  parser = JsonArgumentParser(description = "Standalone Telegram user transport")
  parser.add_argument("--session", help = "Telethon session path override")
  parser.add_argument(
    "--lock",
    help = "Session lock path override for a separate account or hermetic test",
  )
  subparsers = parser.add_subparsers(dest = "command", required = True)

  status = subparsers.add_parser("status", help = "Inspect login/session health")
  status.add_argument("--chat", help = "Optional chat target to resolve")

  login = subparsers.add_parser("login", help = "Log in a real Telegram account")
  login.add_argument("--phone", required = True, help = "Telegram phone number")
  login.add_argument(
    "--secret-stdin",
    choices = ["code", "password"],
    help = "Read exactly one login secret from local stdin",
  )

  precheck = subparsers.add_parser("precheck", help = "Validate the Telegram user session")
  precheck.add_argument("--chat", help = "Optional chat target to resolve")

  send = subparsers.add_parser("send", help = "Send a message as the Telegram user")
  send.add_argument("--chat", required = True, help = "Target chat username or id")
  send.add_argument("--message", help = "Message text, or caption when --media is present")
  send.add_argument("--message-stdin", action = "store_true", help = "Read exact message text from stdin")
  send.add_argument("--message-file", help = "UTF-8 message file; - reads stdin")
  send.add_argument("--caption-file", help = "UTF-8 caption file; - reads stdin")
  send.add_argument("--topic-anchor", type = int, help = "Forum topic anchor; exclusive with --reply-to")
  send.add_argument("--media", help = "Media path or URL to upload")
  send.add_argument("--caption", help = "Caption for --media; overrides --message")
  send.add_argument("--caption-stdin", action = "store_true", help = "Read exact media caption from stdin")
  send.add_argument("--reply-to", type = positive_int, default = 0, help = "Reply-to message id")
  send.add_argument(
    "--voice",
    "--audio-as-voice",
    action = "store_true",
    dest = "voice",
    help = "Send uploaded audio as a Telegram voice note",
  )

  topic_create = subparsers.add_parser("topic-create", help = "Create a forum topic")
  topic_create.add_argument("--chat", required = True, help = "Target forum chat username or id")
  topic_create.add_argument("--title", required = True, help = "Forum topic title")

  topic_delete = subparsers.add_parser("topic-delete", help = "Delete a forum topic by anchor")
  topic_delete.add_argument("--chat", required = True, help = "Target forum chat username or id")
  topic_delete.add_argument("--topic-anchor", type = int, required = True, help = "Forum topic anchor returned by topic-create")

  topic_resolve = subparsers.add_parser("topic-resolve", help = "Resolve an exact forum topic title")
  topic_resolve.add_argument("--chat", required = True, help = "Target forum chat username or id")
  topic_resolve.add_argument("--title", required = True, help = "Exact forum topic title")

  read = subparsers.add_parser("read", help = "Read recent messages and metadata")
  read.add_argument("--chat", required = True, help = "Target chat username or id")
  read.add_argument("--limit", type = int, default = 20, help = "Maximum number of messages")
  read.add_argument("--after-id", type = positive_int, default = 0, help = "Only return newer messages")
  read.add_argument("--before-id", type = positive_int, default = 0, help = "Only return older messages")
  read.add_argument("--contains", default = "", help = "Only return messages containing this substring")
  read.add_argument("--topic-anchor", type = int, help = "Read only this forum topic")

  mark_read = subparsers.add_parser("mark-read", help = "Acknowledge current chat history as read")
  mark_read.add_argument("--chat", required = True, help = "Target chat username or id")

  mark_unread = subparsers.add_parser("mark-unread", help = "Set the dialog unread flag")
  mark_unread.add_argument("--chat", required = True, help = "Target chat username or id")

  download = subparsers.add_parser("download", help = "Download media from one message")
  download.add_argument("--chat", required = True, help = "Target chat username or id")
  download.add_argument("--message-id", type = int, required = True, help = "Message id containing media")
  download.add_argument("--output", required = True, help = "Output file path or directory")

  button_click = subparsers.add_parser(
    "button-click",
    help = "Select one exact inline callback or URL button on one exact message",
  )
  button_click.add_argument("--chat", required = True, help = "Exact target chat username or id")
  button_click.add_argument("--message-id", type = int, required = True, help = "Exact message id")
  button_click.add_argument("--button-text", required = True, help = "Exact visible button text")
  button_click.add_argument(
    "--expected-callback-data",
    help = "Exact UTF-8 callback data expected behind the button",
  )
  button_click.add_argument(
    "--expected-url",
    help = "Exact Telegram public-chat or invite URL to join without opening a browser",
  )

  inbox = subparsers.add_parser("inbox", help = "List dialogs with unread metadata")
  inbox.add_argument("--limit", type = int, default = 20, help = "Maximum number of dialogs")
  inbox.add_argument("--unread", action = "store_true", help = "Only include unread dialogs")
  inbox.add_argument("--dm-only", action = "store_true", help = "Only include direct messages")
  inbox.add_argument("--contains", default = "", help = "Only include matching title, username, or last text")

  subparsers.add_parser("logout", help = "Clear the Telegram user session")
  return parser


async def connect_client(session_path: Path) -> tuple[Any, object]:
  api_id, api_hash = resolve_api_credentials()
  client = create_telegram_client(
    session_path,
    api_id,
    api_hash,
    flood_sleep_threshold = 0,
    request_retries = 0,
    connection_retries = 0,
  )
  # Telethon exposes parse_mode as a property, not a constructor parameter.
  # Disable Markdown interpretation so generated text reaches Telegram literally.
  client.parse_mode = None
  try:
    await client.connect()
    if not await client.is_user_authorized():
      raise PermissionError("Telegram user session is not authorized.")
    me = await client.get_me()
  except BaseException:
    # A connect or identity failure must close SQLite/network resources before
    # the outer context releases the session lock, including cancellation.
    await client.disconnect()
    raise
  return client, me


async def run_precheck(args: argparse.Namespace) -> int:
  session_path = resolve_session_path(args.session)
  if not session_path.exists():
    return fail(
      "E_MISSING_SESSION",
      f"Session file not found at {session_path}.",
      details = {"session_path": str(session_path)},
    )

  async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
    client, me = await connect_client(session_path)
    try:
      chat_payload = None
      if args.chat:
        resolved = await client.get_entity(resolve_chat(args.chat))
        chat_payload = build_chat_payload(resolved)
      return emit(
        {
          "chat": chat_payload,
          "session_path": str(session_path),
          "user": build_user_payload(me),
        }
      )
    finally:
      await client.disconnect()


async def run_status(args: argparse.Namespace) -> int:
  session_path = resolve_session_path(args.session)
  pending_auth = read_pending_auth_state(session_path)

  try:
    resolve_api_credentials()
  except Exception:
    return emit_auth_status(
      chat_payload = None,
      pending_auth = pending_auth,
      session_path = session_path,
      state = "missing_credentials",
      user_payload = None,
    )

  if session_path.exists():
    async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
      api_id, api_hash = resolve_api_credentials()
      client = create_telegram_client(
        session_path,
        api_id,
        api_hash,
        flood_sleep_threshold = 0,
        request_retries = 0,
        connection_retries = 0,
      )
      try:
        await client.connect()
        if await client.is_user_authorized():
          clear_pending_auth_state(session_path)
          me = await client.get_me()
          chat_payload = None
          if args.chat:
            resolved = await client.get_entity(resolve_chat(args.chat))
            chat_payload = build_chat_payload(resolved)
          return emit_auth_status(
            chat_payload = chat_payload,
            pending_auth = None,
            session_path = session_path,
            state = "ready",
            user_payload = build_user_payload(me),
          )
      finally:
        await client.disconnect()

  if pending_auth is not None:
    pending_state = str(pending_auth.get("state") or "").strip()
    if is_valid_pending_auth_state(pending_state):
      return emit_auth_status(
        chat_payload = None,
        pending_auth = pending_auth,
        session_path = session_path,
        state = pending_state,
        user_payload = None,
      )

  if not session_path.exists():
    return emit_auth_status(
      chat_payload = None,
      pending_auth = None,
      session_path = session_path,
      state = "missing_session",
      user_payload = None,
    )

  return emit_auth_status(
    chat_payload = None,
    pending_auth = None,
    session_path = session_path,
    state = "needs_reauth",
    user_payload = None,
  )


async def run_login(args: argparse.Namespace) -> int:
  session_path = resolve_session_path(args.session)
  phone = str(args.phone or "").strip()
  secret = (await read_stdin_bounded(args, single_line=True)).decode("utf-8").rstrip("\r\n") if args.secret_stdin else ""
  code = secret if args.secret_stdin == "code" else ""
  password = secret if args.secret_stdin == "password" else ""
  if not phone:
    return fail("E_USAGE", "Telegram login requires --phone.")

  async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
    api_id, api_hash = resolve_api_credentials()
    client = create_telegram_client(
      session_path,
      api_id,
      api_hash,
      flood_sleep_threshold = 0,
      request_retries = 0,
      connection_retries = 0,
    )
    try:
      await client.connect()
      if await client.is_user_authorized():
        clear_pending_auth_state(session_path)
        me = await client.get_me()
        return emit_auth_status(
          chat_payload = None,
          pending_auth = None,
          session_path = session_path,
          state = "ready",
          user_payload = build_user_payload(me),
        )

      pending_auth = read_pending_auth_state(session_path)
      pending_phone = str((pending_auth or {}).get("phone") or "").strip()
      pending_hash = str((pending_auth or {}).get("phone_code_hash") or "").strip()
      pending_state = str((pending_auth or {}).get("state") or "").strip()

      # Reusing the existing code hash avoids spamming fresh OTP sends while a
      # caller is still in the middle of the same login attempt.
      if (
        not code
        and not password
        and pending_auth is not None
        and pending_phone == phone
        and is_valid_pending_auth_state(pending_state)
      ):
        return emit_auth_status(
          chat_payload = None,
          pending_auth = pending_auth,
          session_path = session_path,
          state = pending_state,
          user_payload = None,
        )

      if not code and not password:
        try:
          stored = await refresh_pending_code_request(client, session_path, phone = phone)
        except Exception as err:
          if classify_login_error(err) != "FloodWaitError":
            raise
          return emit_auth_status(
            chat_payload = None,
            pending_auth = pending_auth,
            session_path = session_path,
            state = pending_state if is_valid_pending_auth_state(pending_state) else "awaiting_code",
            user_payload = None,
            auth_error = "cooldown",
            retry_after_seconds = max(1, int(getattr(err, "seconds", 1) or 1)),
          )
        return emit_auth_status(
          chat_payload = None,
          pending_auth = stored,
          session_path = session_path,
          state = "awaiting_code",
          user_payload = None,
        )

      if not pending_hash or pending_phone != phone:
        return fail(
          "E_LOGIN_CODE_NOT_REQUESTED",
          "No pending Telegram login code was found for this phone. Run login with the phone number first.",
        )

      try:
        if password:
          await client.sign_in(password = password)
        else:
          await client.sign_in(phone = phone, code = code, phone_code_hash = pending_hash)
      except Exception as err:
        error_name = classify_login_error(err)
        if error_name == "SessionPasswordNeededError":
          stored = write_pending_auth_state(
            session_path,
            phone = phone,
            phone_code_hash = pending_hash,
            state = "awaiting_password",
          )
          return emit_auth_status(
            chat_payload = None,
            pending_auth = stored,
            session_path = session_path,
            state = "awaiting_password",
            user_payload = None,
          )
        if error_name in {"PhoneCodeEmptyError", "PhoneCodeInvalidError"}:
          return emit_auth_status(
            chat_payload = None,
            pending_auth = pending_auth,
            session_path = session_path,
            state = "awaiting_code",
            user_payload = None,
            auth_error = "code_invalid",
          )
        if error_name in {
          "PhoneCodeExpiredError",
          "PhoneCodeHashEmptyError",
          "PhoneCodeHashExpiredError",
        }:
          try:
            stored = await refresh_pending_code_request(client, session_path, phone = phone)
          except Exception as refresh_err:
            if classify_login_error(refresh_err) != "FloodWaitError":
              raise
            return emit_auth_status(
              chat_payload = None,
              pending_auth = pending_auth,
              session_path = session_path,
              state = "awaiting_code",
              user_payload = None,
              auth_error = "cooldown",
              retry_after_seconds = max(1, int(getattr(refresh_err, "seconds", 1) or 1)),
            )
          return emit_auth_status(
            chat_payload = None,
            pending_auth = stored,
            session_path = session_path,
            state = "awaiting_code",
            user_payload = None,
            auth_error = "code_expired",
          )
        if error_name == "FloodWaitError":
          return emit_auth_status(
            chat_payload = None,
            pending_auth = pending_auth,
            session_path = session_path,
            state = pending_state,
            user_payload = None,
            auth_error = "cooldown",
            retry_after_seconds = max(1, int(getattr(err, "seconds", 1) or 1)),
          )
        raise

      if not await client.is_user_authorized():
        return fail("E_UNAUTHORIZED_SESSION", "Telegram login completed without an authorized session.")

      clear_pending_auth_state(session_path)
      me = await client.get_me()
      return emit_auth_status(
        chat_payload = None,
        pending_auth = None,
        session_path = session_path,
        state = "ready",
        user_payload = build_user_payload(me),
      )
    finally:
      await client.disconnect()


async def run_send(args: argparse.Namespace) -> int:
  session_path = resolve_session_path(args.session)
  message_text = await resolve_text_payload_async(args, "message")
  media = str(args.media or "").strip()
  caption = await resolve_text_payload_async(args, "caption")
  reply_to = getattr(args, "topic_anchor", None) or args.reply_to or None
  if not message_text.strip() and not media:
    return fail("E_USAGE", "Telegram send requires --message or --media.")
  async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
    client, _ = await connect_client(session_path)
    try:
      # Resolve once so send and the acknowledgement apply to exactly the same
      # dialog while this session owns the lock. A topic is still part of its
      # parent dialog for Telegram read-state purposes.
      chat_entity = resolve_chat(args.chat)
      if getattr(args, "topic_anchor", None):
        # Resolve the anchor against this peer before mutating history. A
        # numeric message from another peer cannot authorize a forum send.
        if functions is None:
          return fail("E_TELETHON_IMPORT", "Telethon forum topic functions are unavailable.")
        topic = await fetch_topic_by_anchor(client, chat=chat_entity, topic_anchor=args.topic_anchor)
        if topic is None:
          return fail("E_TOPIC_NOT_FOUND", "The topic anchor does not belong to the target chat.")
      if media:
        sent = await client.send_file(
          entity = chat_entity,
          file = media,
          caption = caption or message_text or None,
          reply_to = reply_to,
          voice_note = bool(args.voice),
        )
      else:
        sent = await client.send_message(
          entity = chat_entity,
          message = message_text,
          reply_to = reply_to,
        )
      sent = coerce_single_message(sent)
      if sent is None:
        return fail("E_TELEGRAM_USER_BACKEND", "Telegram send returned no message.")
      try:
        # A successful user-account reply resolves the dialog. Keep this in
        # the same client/lock transaction rather than launching a second
        # mark-read process that could race another Telegram operation.
        await client.send_read_acknowledge(chat_entity)
      except Exception as err:
        # The message can already be visible even though the acknowledgement
        # failed. Return an explicit unknown-state failure so callers inspect
        # history instead of blindly retrying and duplicating the send.
        return fail(
          "E_TELEGRAM_USER_SEND_READ_ACK_UNKNOWN",
          "Telegram send may have succeeded, but marking the chat read failed. "
          "Read the target chat before retrying to avoid a duplicate message.",
          details = {"cause": sanitize_error_text(str(err))},
        )
      try:
        chat = await sent.get_chat()
      except Exception as err:
        # The send response already proves a new message exists. Do not mask
        # it with a generic failure that an operator could blindly retry.
        return fail(
          "E_TELEGRAM_USER_SEND_METADATA_UNKNOWN",
          "Telegram send succeeded, but chat metadata lookup failed. Read the target chat before retrying.",
          details={"message_id": int(getattr(sent, "id", 0) or 0), "cause": sanitize_error_text(str(err))},
        )
      return emit({"message": build_message_payload(sent, chat = chat)})
    finally:
      await client.disconnect()


async def run_topic_create(args: argparse.Namespace) -> int:
  if functions is None:
    return fail("E_TELETHON_IMPORT", "Telethon forum topic functions are unavailable.")
  session_path = resolve_session_path(args.session)
  title = str(args.title or "").strip()
  if not title:
    return fail("E_USAGE", "Telegram topic-create requires --title.")
  async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
    client, _ = await connect_client(session_path)
    try:
      updates = await client(
        functions.messages.CreateForumTopicRequest(
          peer = resolve_chat(args.chat),
          title = title,
        )
      )
      message = extract_created_topic_message(updates)
      if message is None:
        return fail(
          "E_TOPIC_CREATE_NO_ANCHOR",
          f"Telegram topic-create returned no topic anchor for {title}.",
        )
      return emit(build_topic_create_payload(chat_raw = args.chat, message = message, title = title))
    finally:
      await client.disconnect()


async def run_topic_delete(args: argparse.Namespace) -> int:
  if functions is None:
    return fail("E_TELETHON_IMPORT", "Telethon forum topic functions are unavailable.")
  session_path = resolve_session_path(args.session)
  topic_anchor = int(args.topic_anchor or 0)
  if topic_anchor <= 0:
    return fail("E_USAGE", "Telegram topic-delete requires --topic-anchor.")
  async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
    client, _ = await connect_client(session_path)
    try:
      result = await client(
        functions.messages.DeleteTopicHistoryRequest(
          peer = resolve_chat(args.chat),
          top_msg_id = topic_anchor,
        )
      )
      return emit(
        build_topic_delete_payload(
          chat_raw = args.chat,
          result = result,
          topic_anchor = topic_anchor,
        )
      )
    finally:
      await client.disconnect()


async def run_topic_resolve(args: argparse.Namespace) -> int:
  if functions is None:
    return fail("E_TELETHON_IMPORT", "Telethon forum topic functions are unavailable.")
  session_path = resolve_session_path(args.session)
  title = str(args.title or "").strip()
  if not title:
    return fail("E_USAGE", "Telegram topic-resolve requires --title.")
  async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
    client, _ = await connect_client(session_path)
    try:
      # Telegram applies q server-side. The exact normalized comparison below
      # prevents a fuzzy result from silently selecting the wrong topic.
      # A short server page is not an end-of-results signal: Telegram may return
      # fewer topics than requested while ForumTopics.count still advertises
      # more. Enumerate the complete search result before declaring one exact
      # title unique, using the last topic's top message as the next cursor.
      chat = resolve_chat(args.chat)
      offset_date = None
      offset_id = 0
      offset_topic = 0
      seen_topic_ids: set[int] = set()
      matches = []
      while True:
        result = await client(
          functions.messages.GetForumTopicsRequest(
            peer = chat,
            offset_date = offset_date,
            offset_id = offset_id,
            offset_topic = offset_topic,
            limit = 100,
            q = title,
          )
        )
        topics = list(getattr(result, "topics", []) or [])
        expected_count = max(0, int(getattr(result, "count", 0) or 0))
        new_topics = []
        for topic in topics:
          topic_id = int(getattr(topic, "id", 0) or 0)
          if topic_id <= 0 or topic_id in seen_topic_ids:
            continue
          seen_topic_ids.add(topic_id)
          new_topics.append(topic)
          if (
            getattr(topic, "title", None) is not None
            and str(getattr(topic, "title", "")).strip().casefold() == title.casefold()
          ):
            matches.append(topic)
        if len(matches) > 1:
          break
        if len(seen_topic_ids) >= expected_count:
          break
        if not new_topics:
          return fail(
            "E_TOPIC_RESOLUTION_INCOMPLETE",
            "Telegram did not return enough forum topics to prove the exact title is unique.",
            details = {"expected_count": expected_count, "received_count": len(seen_topic_ids)},
          )

        # Telegram defines the cursor from the last returned topic, even if a
        # page repeats an already-seen entry.
        last_topic = topics[-1]
        next_offset_topic = int(getattr(last_topic, "id", 0) or 0)
        next_offset_id = int(getattr(last_topic, "top_message", 0) or 0)
        next_offset_message = next(
          (
            message
            for message in getattr(result, "messages", []) or []
            if int(getattr(message, "id", 0) or 0) == next_offset_id
          ),
          None,
        )
        next_offset_date = getattr(next_offset_message, "date", None)
        next_cursor = (next_offset_date, next_offset_id, next_offset_topic)
        if (
          next_offset_topic <= 0
          or next_offset_id <= 0
          or next_offset_date is None
          or next_cursor == (offset_date, offset_id, offset_topic)
        ):
          return fail(
            "E_TOPIC_RESOLUTION_INCOMPLETE",
            "Telegram returned an incomplete forum-topic pagination cursor.",
            details = {"expected_count": expected_count, "received_count": len(seen_topic_ids)},
          )
        offset_date, offset_id, offset_topic = next_cursor
      if not matches:
        return fail(
          "E_TOPIC_NOT_FOUND",
          "No forum topic with that exact title belongs to the target chat.",
        )
      if len(matches) > 1:
        return fail(
          "E_TOPIC_AMBIGUOUS",
          "Multiple forum topics in the target chat have that exact title; use an explicit topic anchor.",
          details = {"topic_anchors": [int(getattr(topic, "id", 0) or 0) for topic in matches]},
        )
      return emit({"chat": args.chat, "topic": build_topic_payload(matches[0])})
    finally:
      await client.disconnect()


async def run_read(args: argparse.Namespace) -> int:
  session_path = resolve_session_path(args.session)
  limit = max(1, min(int(args.limit or 20), 200))
  contains = str(args.contains or "")
  topic_anchor_raw = getattr(args, "topic_anchor", None)
  topic_anchor = int(topic_anchor_raw or 0)
  if topic_anchor_raw is not None and topic_anchor <= 0:
    return fail("E_USAGE", "Telegram read --topic-anchor must be a positive integer.")
  async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
    client, _ = await connect_client(session_path)
    try:
      chat = resolve_chat(args.chat)
      topic = None
      if topic_anchor:
        if functions is None:
          return fail("E_TELETHON_IMPORT", "Telethon forum topic functions are unavailable.")
        topic = await fetch_topic_by_anchor(client, chat = chat, topic_anchor = topic_anchor)
        if topic is None:
          return fail(
            "E_TOPIC_NOT_FOUND",
            "The topic anchor does not belong to the target chat.",
            details = {"topic_anchor": topic_anchor},
          )
        # GetReplies is Telegram's topic-scoped history RPC. Unlike a chat-wide
        # scan plus local matching, it cannot mix sibling forum topics.
        scan_cap = compute_message_scan_cap(contains = contains, limit = limit)
        replies_by_id: dict[int, object] = {}
        offset_id = 0
        offset_date = None
        while len(replies_by_id) < scan_cap:
          # Telegram may clamp or return a short replies page even when more
          # messages exist. Keep the same server-side topic and id bounds on
          # every request, and advance with the oldest message as the cursor.
          result = await client(
            functions.messages.GetRepliesRequest(
              peer = chat,
              msg_id = topic_anchor,
              offset_id = offset_id,
              offset_date = offset_date,
              add_offset = 0,
              limit = scan_cap - len(replies_by_id),
              max_id = int(args.before_id or 0),
              min_id = int(args.after_id or 0),
              hash = 0,
            )
          )
          page = list(getattr(result, "messages", []) or [])
          if not page:
            break

          # Every object in a backend page must be provably part of this
          # bounded query before it can affect the cursor or scan cap. Dropping
          # one malformed or out-of-bound object would silently truncate the
          # caller's requested topic window.
          page_with_ids: list[tuple[int, object]] = []
          for message in page:
            raw_message_id = getattr(message, "id", None)
            if (
              not isinstance(raw_message_id, int)
              or isinstance(raw_message_id, bool)
              or raw_message_id <= 0
            ):
              return fail(
                "E_TOPIC_READ_INCOMPLETE",
                "Telegram returned a topic replies page with an invalid message id.",
                details = {"received_count": len(replies_by_id), "scan_cap": scan_cap},
              )
            message_id = raw_message_id
            if args.after_id and message_id <= int(args.after_id):
              return fail(
                "E_TOPIC_READ_INCOMPLETE",
                "Telegram returned a topic reply outside the requested lower bound.",
                details = {
                  "after_id": int(args.after_id),
                  "message_id": message_id,
                  "received_count": len(replies_by_id),
                },
              )
            if args.before_id and message_id >= int(args.before_id):
              return fail(
                "E_TOPIC_READ_INCOMPLETE",
                "Telegram returned a topic reply outside the requested upper bound.",
                details = {
                  "before_id": int(args.before_id),
                  "message_id": message_id,
                  "received_count": len(replies_by_id),
                },
              )
            page_with_ids.append((message_id, message))

          # A non-empty page without a strictly older cursor cannot safely
          # continue. Fail closed instead of returning partial topic history or
          # looping on Telegram's response.
          next_offset_id, oldest_message = min(page_with_ids, key = lambda item: item[0])
          if offset_id > 0 and next_offset_id >= offset_id:
            return fail(
              "E_TOPIC_READ_INCOMPLETE",
              "Telegram did not advance the topic replies pagination cursor.",
              details = {
                "offset_id": offset_id,
                "received_count": len(replies_by_id),
                "scan_cap": scan_cap,
              },
            )
          # Keep the newest replies when an over-returning or future server
          # response exceeds the requested remainder; the public scan cap is
          # exact.
          sorted_page = sorted(page_with_ids, key = lambda item: item[0], reverse = True)
          for message_id, message in sorted_page:
            if message_id not in replies_by_id and len(replies_by_id) >= scan_cap:
              break
            replies_by_id[message_id] = message
          offset_id = next_offset_id
          offset_date = getattr(oldest_message, "date", None)

        replies = list(replies_by_id.values())
        # GetReplies owns the strict topic scope, but Telegram does not promise
        # that every response page repeats the topic's creation/root message.
        # Fetch that one exact id separately so root-only reads stay complete.
        root = await client.get_messages(chat, ids = topic_anchor)
        messages_by_id = {
          int(getattr(message, "id", 0) or 0): message
          for message in [*replies, root]
          if message is not None and int(getattr(message, "id", 0) or 0) > 0
        }
        messages = sorted(
          messages_by_id.values(),
          key = lambda message: int(getattr(message, "id", 0) or 0),
          reverse = True,
        )
      else:
        read_kwargs = {
          "limit": compute_message_scan_cap(contains = contains, limit = limit),
        }
        if args.after_id:
          read_kwargs["min_id"] = int(args.after_id)
        if args.before_id:
          read_kwargs["max_id"] = int(args.before_id)
        messages = await client.get_messages(chat, **read_kwargs)
      normalized = []
      for message in messages:
        message_id = int(getattr(message, "id", 0) or 0)
        if args.after_id and message_id <= args.after_id:
          continue
        if args.before_id and message_id >= args.before_id:
          continue
        if not message_matches_text(message, contains = contains):
          continue
        normalized.append(build_message_payload(message))
        if len(normalized) >= limit:
          break
      payload: dict[str, object] = {"messages": normalized}
      if topic is not None:
        payload["topic"] = build_topic_payload(topic)
      return emit(payload)
    finally:
      await client.disconnect()


async def run_mark_read(args: argparse.Namespace) -> int:
  session_path = resolve_session_path(args.session)
  async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
    client, _ = await connect_client(session_path)
    try:
      # Omitting a message/max_id tells Telethon to acknowledge the current
      # history boundary, matching Telegram's "mark chat read" behavior.
      await client.send_read_acknowledge(resolve_chat(args.chat))
      return emit({"chat": args.chat, "marked_read": True})
    finally:
      await client.disconnect()


async def run_mark_unread(args: argparse.Namespace) -> int:
  if functions is None:
    return fail("E_TELETHON_IMPORT", "Telethon dialog read-state functions are unavailable.")
  session_path = resolve_session_path(args.session)
  async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
    client, _ = await connect_client(session_path)
    try:
      # Telegram models this as a dialog-level unread flag, not as rewinding
      # message history or changing the server's read acknowledgement.
      await client(
        functions.messages.MarkDialogUnreadRequest(
          peer = resolve_chat(args.chat),
          unread = True,
        )
      )
      return emit({"chat": args.chat, "marked_unread": True})
    finally:
      await client.disconnect()


async def run_download(args: argparse.Namespace) -> int:
  session_path = resolve_session_path(args.session)
  message_id = int(args.message_id or 0)
  output_raw = str(args.output or "").strip()
  if message_id <= 0:
    return fail("E_USAGE", "Telegram download requires --message-id.")
  if not output_raw:
    return fail("E_USAGE", "Telegram download requires --output.")

  async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
    client, _ = await connect_client(session_path)
    try:
      message = await client.get_messages(resolve_chat(args.chat), ids = message_id)
      if message is None:
        return fail("E_MESSAGE_NOT_FOUND", f"Telegram message {message_id} was not found.")
      if getattr(message, "media", None) is None:
        return fail("E_NO_MEDIA", f"Telegram message {message_id} has no downloadable media.")
      output_path = resolve_download_output_path(output_raw, chat_raw = args.chat, message = message)
      downloaded = await client.download_media(message, file = str(output_path))
      if not downloaded:
        return fail("E_DOWNLOAD_FAILED", f"Telegram media download returned no file for message {message_id}.")
      downloaded_path = Path(str(downloaded)).expanduser()
      size_bytes = downloaded_path.stat().st_size if downloaded_path.exists() else None
      message_payload = build_message_payload(message)
      return emit(
        {
          "chat": args.chat,
          "message": message_payload,
          "message_id": message_id,
          "media_kind": message_payload.get("media_kind"),
          "path": str(downloaded_path),
          "size_bytes": size_bytes,
        }
      )
    finally:
      await client.disconnect()


async def run_button_click(args: argparse.Namespace) -> int:
  session_path = resolve_session_path(args.session)
  message_id = int(args.message_id or 0)
  button_text = str(args.button_text or "")
  callback_data = str(getattr(args, "expected_callback_data", None) or "")
  expected_url = str(getattr(args, "expected_url", None) or "")
  if message_id <= 0:
    return fail("E_USAGE", "Telegram button-click requires a positive --message-id.")
  if not button_text:
    return fail("E_USAGE", "Telegram button-click requires --button-text.")
  if bool(callback_data) == bool(expected_url):
    return fail(
      "E_USAGE",
      "Telegram button-click requires exactly one of --expected-callback-data or --expected-url.",
    )

  expected_callback_bytes = callback_data.encode("utf-8")
  async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
    client, _ = await connect_client(session_path)
    try:
      # Fetch by explicit id from the explicit chat. Never scan recent history:
      # queue/steer proof must stay tied to the one message the caller observed.
      message = await client.get_messages(resolve_chat(args.chat), ids = message_id)
      if message is None or int(getattr(message, "id", 0) or 0) != message_id:
        return fail(
          "E_MESSAGE_NOT_FOUND",
          f"Telegram message {message_id} was not found in the requested chat.",
        )

      available = list_message_buttons(message)
      matches = [
        (button, metadata)
        for button, metadata in available
        if str(getattr(button, "text", "") or "") == button_text
        and (
          getattr(button, "data", None) == expected_callback_bytes
          if callback_data
          else str(getattr(button, "url", "") or "") == expected_url
        )
      ]
      if len(matches) != 1:
        # Fail closed and return enough bounded metadata to diagnose stale UI
        # assumptions. Crucially, no Message.click call occurs on this branch.
        return fail(
          "E_BUTTON_MISMATCH",
          "Expected exactly one inline button matching text and the selected callback or URL guard.",
          details = {
            "available_buttons": [metadata for _, metadata in available[:100]],
            "match_count": len(matches),
            "message_id": message_id,
            "truncated": len(available) > 100,
          },
        )

      _, matched = matches[0]
      if expected_url:
        telegram_action = parse_telegram_join_url(expected_url)
        if telegram_action is None:
          # Exact matching proves which URL the caller selected, but arbitrary
          # web navigation remains outside this MTProto command's authority.
          return emit(
            {
              "button": matched,
              "chat": args.chat,
              "click_result": build_button_click_result_payload(expected_url),
              "clicked": False,
              "message_id": message_id,
              "url_action": {
                "kind": "unsupported",
                "status": "action_required",
                "url": expected_url,
              },
              "url_action_required": True,
            }
          )
        url_action = await run_telegram_url_action(client, url = expected_url)
        return emit(
          {
            "button": matched,
            "chat": args.chat,
            "click_result": build_button_click_result_payload(expected_url),
            "clicked": True,
            "message_id": message_id,
            "url_action": url_action,
          }
        )

      # Coordinates are derived only after the exact text+value uniqueness check.
      # They are not accepted from callers and cannot select a latest/nearby UI.
      click_result = await message.click(i = matched["row"], j = matched["column"])
      return emit(
        {
          "button": matched,
          "chat": args.chat,
          "click_result": build_button_click_result_payload(click_result),
          "clicked": True,
          "message_id": message_id,
        }
      )
    finally:
      await client.disconnect()


async def run_inbox(args: argparse.Namespace) -> int:
  session_path = resolve_session_path(args.session)
  limit = max(1, min(int(args.limit or 20), 200))
  contains = str(args.contains or "")
  async with acquire_session_lock_async(session_path, lock_path_override = getattr(args, "lock", None)):
    client, _ = await connect_client(session_path)
    try:
      dialogs = []
      scan_cap = compute_inbox_scan_cap(
        contains = contains,
        limit = limit,
        dm_only = bool(args.dm_only),
        unread_only = bool(args.unread),
      )
      # Keep the scan bounded, but do not assume the first few hundred dialogs are
      # representative once filters are applied client-side.
      async for dialog in client.iter_dialogs(limit = scan_cap, ignore_pinned = False):
        if not dialog_matches_inbox_filters(
          dialog,
          contains = contains,
          dm_only = bool(args.dm_only),
          unread_only = bool(args.unread),
        ):
          continue
        dialogs.append(build_dialog_payload(dialog))
        if len(dialogs) >= limit:
          break
      return emit({"dialogs": dialogs})
    finally:
      await client.disconnect()


async def run_logout(args: argparse.Namespace) -> int:
  session_path = resolve_session_path(args.session)
  async with acquire_session_lock_async(session_path, lock_path_override=getattr(args, "lock", None)):
    removed_paths = clear_session_artifacts(session_path)
  return emit({"cleared": bool(removed_paths), "removed_paths": removed_paths, "session_path": str(session_path)})


async def run(args: argparse.Namespace | None = None) -> int:
  if args is None:
    try:
      args = build_parser().parse_args()
    except ValueError as err:
      return fail("E_USAGE", sanitize_error_text(str(err)))

  try:
    validate_args(args)
    if args.command == "status":
      return await run_status(args)
    if args.command == "logout":
      return await run_logout(args)
    resolve_api_credentials()
    if args.command == "precheck":
      return await run_precheck(args)
    if args.command == "login":
      return await run_login(args)
    if args.command == "send":
      return await run_send(args)
    if args.command == "topic-create":
      return await run_topic_create(args)
    if args.command == "topic-delete":
      return await run_topic_delete(args)
    if args.command == "topic-resolve":
      return await run_topic_resolve(args)
    if args.command == "read":
      return await run_read(args)
    if args.command == "mark-read":
      return await run_mark_read(args)
    if args.command == "mark-unread":
      return await run_mark_unread(args)
    if args.command == "download":
      return await run_download(args)
    if args.command == "button-click":
      return await run_button_click(args)
    if args.command == "inbox":
      return await run_inbox(args)
    return fail("E_USAGE", f"Unsupported command: {args.command}")
  except InputTimeoutError as err:
    return fail("E_INPUT_TIMEOUT", sanitize_error_text(str(err)))
  except TimeoutError as err:
    return fail("E_SESSION_LOCK_TIMEOUT", sanitize_error_text(str(err)))
  except PermissionError as err:
    return fail("E_UNAUTHORIZED_SESSION", sanitize_error_text(str(err)))
  except ValueError as err:
    return fail("E_USAGE", sanitize_error_text(str(err)))
  except Exception as err:  # pragma: no cover - script-level fallback
    return fail("E_TELEGRAM_USER_BACKEND", sanitize_error_text(str(err)))


def main() -> None:
  try:
    raise SystemExit(asyncio.run(run()))
  except KeyboardInterrupt:
    raise SystemExit(130) from None


if __name__ == "__main__":
  main()
