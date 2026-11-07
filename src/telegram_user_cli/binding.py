"""One optional, non-secret account binding; delegates to the existing CLI via exec."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from . import __version__


HELP = """Usage: telegram-user-bound <command> [arguments]
       telegram-user-bound --check-binding

Runs the existing telegram-user CLI with one operator-selected binding.
Configuration: $TELEGRAM_USER_BINDING (absolute JSON path), otherwise
$XDG_CONFIG_HOME/telegram-user/binding.json, default ~/.config/telegram-user/binding.json.

Required non-secret JSON (version 1):
  {"version": 1, "env_file": "/private/account.env",
   "session": "/private/account.session",
   "shared_locks": ["/private/machine.lock", "/private/legacy.lock"]}

Paths must be absolute; session must end in .session. shared_locks must be explicit
(an empty list is only appropriate after all shared-lock users have transitioned).
Do not put credential values in this file. Existing credentials are read only by
the normal CLI from env_file. Child TELEGRAM_API_ID/TELEGRAM_API_HASH are removed.
Caller --env-file/--session/--lock/--shared-lock overrides are rejected.

--check-binding validates configuration only; it never opens credentials, sessions,
or locks. --help needs no binding. For command options: telegram-user <command> --help.
Examples: telegram-user-bound inbox --unread --limit 200 --scan-limit 3000 --json
          telegram-user-bound read --chat TARGET --limit 5 --format compact
No migration, installation, account discovery, login, or automatic retry occurs.
"""


def fail(code: str, message: str) -> int:
    print(json.dumps({"error": {"code": code, "message": message}}), file=sys.stderr)
    return 2


def config_path() -> Path:
    explicit = os.environ.get("TELEGRAM_USER_BINDING")
    if explicit is not None:
        path = Path(explicit)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        path = base / "telegram-user" / "binding.json"
    if not path.is_absolute():
        raise ValueError("Binding configuration path must be absolute.")
    return path


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate binding keys are not allowed.")
        result[key] = value
    return result


def absolute_path(value) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Binding selectors must be nonempty absolute paths without control characters.")
    if not Path(value).is_absolute():
        raise ValueError("Binding selectors must be absolute paths.")
    return value


def load_binding() -> dict:
    # Only this non-secret config is opened. No existence probes or reads of targets.
    with config_path().open("rb") as stream:
        raw = stream.read(16_385)
    if len(raw) > 16_384:
        raise ValueError("Binding exceeds the 16 KiB configuration limit.")
    data = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(data, dict) or set(data) != {"version", "env_file", "session", "shared_locks"}:
        raise ValueError("Binding requires exactly version, env_file, session and shared_locks.")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Unsupported binding version.")
    for name in ("env_file", "session"):
        data[name] = absolute_path(data[name])
    # Avoid the transport's automatic suffix changing the selected database identity.
    if not data["session"].endswith(".session"):
        raise ValueError("Binding session must end in .session.")
    if not isinstance(data["shared_locks"], list):
        raise ValueError("Binding shared_locks must be an explicit list.")
    data["shared_locks"] = [absolute_path(path) for path in data["shared_locks"]]
    return data


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv in (["--help"], ["-h"]):
        print(HELP, end="")
        return 0
    if argv == ["--version"]:
        print(f"telegram-user-bound {__version__}")
        return 0
    if argv != ["--check-binding"]:
        from .cli import build_parser, normalize_globals

        try:
            # Use the real parser: payloads that resemble options remain payloads,
            # while parsed selector overrides (including abbreviations) fail closed.
            args = build_parser().parse_args(normalize_globals(argv))
        except ValueError:
            return fail("E_USAGE", "Invalid command. Use telegram-user <command> --help.")
        if args.env_file is not None or args.session is not None or args.lock is not None or args.shared_lock:
            return fail("E_BINDING_OVERRIDE", "Account/session/lock overrides are disabled; use telegram-user for explicit selectors.")
    try:
        data = load_binding()
    except FileNotFoundError:
        return fail("E_BINDING_CONFIG", "Binding file not found. Run telegram-user-bound --help for configuration.")
    except PermissionError:
        return fail("E_BINDING_CONFIG", "Binding file is not readable; ask its operator to check access.")
    except (json.JSONDecodeError, UnicodeError):
        return fail("E_BINDING_CONFIG", "Binding is not valid JSON; credential values do not belong in it.")
    except ValueError as error:
        # These messages come only from our fixed schema checks, never config values.
        return fail("E_BINDING_CONFIG", str(error))
    except OSError:
        return fail("E_BINDING_CONFIG", "Could not read binding configuration; no operation was started.")
    if argv == ["--check-binding"]:
        print(json.dumps({"ok": True, "binding": data, "per_session_lock": data["session"] + ".lock"}))
        return 0
    prefix = ["--env-file", data["env_file"], "--session", data["session"],
              "--lock", data["session"] + ".lock"]
    for lock in data["shared_locks"]:
        prefix.extend(["--shared-lock", lock])
    environment = os.environ.copy()
    environment.pop("TELEGRAM_API_ID", None)
    environment.pop("TELEGRAM_API_HASH", None)
    try:
        # No shell, child supervisor, retry, stdin read, or deadline of our own.
        # exec preserves PID, file descriptors, signals and the caller's deadline.
        # Pin the package root already selected by the console/source launcher.
        # Unlike -m, this replaces Python's implicit cwd search entry without
        # rewriting PYTHONPATH or any unrelated child environment value.
        bootstrap = ("import sys; sys.path[0]=sys.argv.pop(1); import runpy; "
                     "runpy.run_module('telegram_user_cli',run_name='__main__')")
        package_root = str(Path(__file__).resolve().parent.parent)
        os.execve(sys.executable,
                  [sys.executable, "-c", bootstrap, package_root, *prefix, *argv], environment)
    except OSError:
        return fail("E_BINDING_EXEC", "Could not execute the existing CLI; no operation was retried.")
    return 0  # Reachable only when exec is replaced by an offline test double.


if __name__ == "__main__":
    raise SystemExit(main())
