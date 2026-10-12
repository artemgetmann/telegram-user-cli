"""Small operator layer around the independently usable Telegram transport."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import getpass
import io
import json
import math
import os
from pathlib import Path
import sys
import time

from dotenv import load_dotenv

from . import __version__, backend


def positive_seconds(value: str) -> float:
    """Reject NaN and infinity so a requested deadline is actually bounded."""
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return seconds


def normalize_globals(argv: list[str]) -> list[str]:
    """Accept common flags on either side of the verb, without rewriting payloads.

    Only recognized option tokens are moved. Values are consumed with their
    option, so a message that looks like a global flag remains literal text.
    """
    takes_value = {"--session", "--lock", "--env-file", "--rpc-timeout"}
    local_values = {
        "--chat", "--message", "--message-file", "--caption", "--caption-file",
        "--media", "--reply-to", "--topic-anchor", "--title", "--limit",
        "--after-id", "--before-id", "--contains", "--phone", "--secret-stdin",
        "--message-id", "--output", "--button-text", "--expected-callback-data",
        "--expected-url", "--sender-id", "--timeout", "--poll-interval",
    }
    global_args: list[str] = []
    local_args: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            local_args.extend(argv[index:])
            break
        flag = token.split("=", 1)[0]
        destination = global_args if flag in takes_value or token == "--json" else local_args
        destination.append(token)
        if "=" not in token and flag in takes_value | local_values and index + 1 < len(argv):
            destination.append(argv[index + 1])
            index += 1
        index += 1
    return global_args + local_args


def build_parser() -> argparse.ArgumentParser:
    parser = backend.build_parser()
    parser.prog = "telegram-user"
    parser.description = "Use your own Telegram account from the terminal or an agent."
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--json", action="store_true", help="JSON is the default output format")
    parser.add_argument("--env-file", help="Load credentials from this explicit env file")
    parser.add_argument("--rpc-timeout", type=positive_seconds, default=60,
                        help="Deadline per operation in seconds (default: 60)")
    # Extend the transport's parser rather than duplicating its argument contract.
    commands = next(action for action in parser._actions
                    if isinstance(action, argparse._SubParsersAction))
    commands.add_parser("doctor", help="Explain local setup without network access")
    commands.add_parser("skill", help="Print the bundled agent instructions")
    wait = commands.add_parser("wait", help="Wait for a new message in one exact chat")
    wait.add_argument("--chat", required=True)
    wait.add_argument("--after-id", type=int, required=True, help="Last observed message id")
    wait.add_argument("--sender-id", type=int)
    wait.add_argument("--contains", default="")
    wait.add_argument("--topic-anchor", type=int)
    wait.add_argument("--timeout", type=positive_seconds, default=60)
    wait.add_argument("--poll-interval", type=positive_seconds, default=3)
    return parser


def load_credentials(env_file: str | None) -> None:
    """Never search the working directory or adopt another app's credentials."""
    if env_file:
        path = Path(env_file).expanduser()
        if not path.is_file():
            raise ValueError("The explicit --env-file does not exist or is not a regular file.")
        # Process environment intentionally wins over an env-file value.
        load_dotenv(path, override=False, interpolate=False)


def doctor(args: argparse.Namespace) -> int:
    """A file on disk is not proof of authentication; status performs that check."""
    configured = True
    try:
        backend.resolve_api_credentials()
    except ValueError:
        configured = False
    path = backend.resolve_session_path(args.session)
    state = "missing_credentials" if not configured else (
        "auth_unverified" if path.is_file() else "missing_session"
    )
    return backend.emit({
        "state": state,
        "credentials_configured": configured,
        "session_exists": path.is_file(),
        "session_path": str(path),
        "next_step": {
            "missing_credentials": "Set TELEGRAM_API_ID and TELEGRAM_API_HASH from my.telegram.org/apps.",
            "missing_session": "Run telegram-user login --phone YOUR_PHONE_NUMBER.",
            "auth_unverified": "Run telegram-user status --json to check authentication with Telegram.",
        }[state],
    })


async def invoke(args: argparse.Namespace) -> int:
    """Bound the whole operation, including connect, RPC, and cleanup."""
    try:
        return await asyncio.wait_for(backend.run(args), timeout=args.rpc_timeout)
    except asyncio.TimeoutError:
        return backend.fail(
            "E_TIMEOUT",
            "Telegram operation exceeded its deadline. A mutation may have succeeded; "
            "read the exact target before retrying.",
        )


async def capture(args: argparse.Namespace) -> tuple[int, dict]:
    """Capture one backend result; prompts and errors remain on stderr."""
    output = io.StringIO()
    with redirect_stdout(output):
        code = await invoke(args)
    if code:
        return code, {}
    return code, json.loads(output.getvalue())


async def interactive_login(args: argparse.Namespace) -> int:
    """Prompt only on a terminal; agents use the explicit staged stdin contract."""
    code, result = await capture(args)
    while code == 0 and result.get("state") in {"awaiting_code", "awaiting_password"}:
        state = result["state"]
        # Read secrets with terminal echo disabled, and never put them in argv.
        secret = getpass.getpass("Telegram code: " if state == "awaiting_code"
                                 else "Telegram 2FA password: ", stream=sys.stderr)
        args.secret_stdin = "code" if state == "awaiting_code" else "password"
        previous_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(secret + "\n")
            code, result = await capture(args)
        finally:
            sys.stdin = previous_stdin
            secret = ""
    if code == 0:
        backend.emit(result)
    return code


async def wait_for_message(args: argparse.Namespace) -> int:
    """Poll reads only, and never turn an observed message into an automatic reply."""
    if args.after_id < 0 or (args.sender_id is not None and args.sender_id <= 0):
        return backend.fail("E_USAGE", "--after-id must be nonnegative and --sender-id positive.")
    read_args = argparse.Namespace(**vars(args))
    read_args.command = "read"
    read_args.limit = 100
    read_args.before_id = 0
    # Filter locally so an empty filtered transport page cannot hide an older
    # inbound reply from the requested sender. Walk every page back to after_id.
    read_args.contains = ""
    deadline = time.monotonic() + args.timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return backend.emit({"matched": False, "timed_out": True, "messages": []})
        read_args.rpc_timeout = min(args.rpc_timeout, remaining)
        code, result = await capture(read_args)
        if code:
            return code
        messages = result.get("messages", [])
        matches = [message for message in messages
                   if not message.get("out") and int(message.get("message_id", 0)) > args.after_id
                   and (args.sender_id is None or message.get("sender_id") == args.sender_id)
                   and args.contains.casefold() in str(message.get("text", "")).casefold()]
        if matches:
            return backend.emit({"matched": True, "timed_out": False, "messages": matches})
        if len(messages) >= read_args.limit:
            oldest = min(int(message.get("message_id", 0)) for message in messages)
            if oldest > args.after_id and (not read_args.before_id or oldest < read_args.before_id):
                read_args.before_id = oldest
                continue
            return backend.fail("E_WAIT_INCOMPLETE", "Message history did not advance; reply coverage is incomplete.")
        # A short page proves this poll covered the requested interval. Start
        # from the newest page again after the delay to observe newly arriving replies.
        read_args.before_id = 0
        await asyncio.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))


async def dispatch(args: argparse.Namespace) -> int:
    if args.command == "doctor":
        return doctor(args)
    if args.command == "skill":
        print(Path(__file__).with_name("SKILL.md").read_text(encoding="utf-8"), end="")
        return 0
    load_credentials(args.env_file)
    if args.command == "wait":
        return await wait_for_message(args)
    if args.command == "login" and not args.secret_stdin and sys.stdin.isatty():
        return await interactive_login(args)
    return await invoke(args)


def main() -> None:
    parser = build_parser()
    try:
        args = parser.parse_args(normalize_globals(sys.argv[1:]))
        # Apply a private umask before Telethon or a lock creates credential files.
        os.umask(0o077)
        if args.command == "doctor":
            load_credentials(args.env_file)
        raise SystemExit(asyncio.run(dispatch(args)))
    except (ValueError, OSError) as error:
        raise SystemExit(backend.fail("E_USAGE", backend.sanitize_error_text(str(error)))) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
