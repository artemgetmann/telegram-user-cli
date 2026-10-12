"""Replay the exact-text workflow with synthetic data; never connects to Telegram."""

import asyncio
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from telethon.tl.types import User
from telegram_user_cli import backend, cli


class DemoClient:
    """The transport seam uses a fixture instead of anyone's real account."""

    def __init__(self):
        self.peer = User(id=123, first_name="Demo")
        self.message = None

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return self.peer

    async def send_message(self, *, message, **kwargs):
        self.message = SimpleNamespace(
            id=123, chat_id=123, sender_id=123, out=True, message=message,
            date=datetime(2026, 1, 1, tzinfo=timezone.utc), get_chat=self.get_me,
        )
        return self.message

    async def send_read_acknowledge(self, *args):
        pass

    async def get_messages(self, *args, **kwargs):
        return [self.message]


async def main():
    print("OFFLINE FIXTURE DEMO. NO TELEGRAM CONNECTION OR REAL RECIPIENTS.\n")
    client = DemoClient()
    text = "hello from my agent\nliteral path: C:\\notes\\telegram.txt\n"
    with tempfile.TemporaryDirectory(prefix="telegram-user-demo-") as directory:
        message = Path(directory) / "message.txt"
        message.write_text(text, encoding="utf-8")
        common = ["--session", str(Path(directory) / "demo.session")]
        commands = [
            ("telegram-user send --chat me --message-file message.txt",
             ["send", "--chat", "me", "--message-file", str(message)]),
            ("telegram-user read --chat me --limit 1", ["read", "--chat", "me", "--limit", "1"]),
        ]
        with patch.object(backend, "resolve_api_credentials", return_value=(123, "synthetic")), \
                patch.object(backend, "create_telegram_client", return_value=client):
            for shown, command in commands:
                print("$ " + shown)
                args = cli.build_parser().parse_args(common + command)
                captured = io.StringIO()
                with redirect_stdout(captured):
                    result = await cli.invoke(args)
                if result:
                    raise SystemExit(result)
                record = json.loads(captured.getvalue())
                message_record = record.get("message") or record["messages"][0]
                print(json.dumps({"message_id": message_record["message_id"],
                                  "text": message_record["text"]}, ensure_ascii=False))
                assert message_record["text"] == text, "Exact text changed during the workflow"
                print()
    print("Exact multiline text and literal backslashes survived send and readback.")


if __name__ == "__main__":
    asyncio.run(main())
