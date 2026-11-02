"""Offline adoption contracts; no account data, network, or real runtime locks."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon.tl.types import PeerChannel, PeerChat, PeerUser

from telegram_user_cli import backend as b, cli


def dialog(index, *, unread=False, text="fixture", kind="user"):
    peer = {"user": PeerUser, "chat": PeerChat, "channel": PeerChannel}[kind](index)
    # Real peer classes exercise Telethon's marked-ID conversion.
    peer.id = index
    return SimpleNamespace(
        entity=peer, name="Fixture", is_user=kind == "user", is_channel=kind == "channel",
        is_group=kind != "user", unread_count=int(unread), pinned=index == 1,
        archived=index % 2 == 0,
        message=SimpleNamespace(id=index, message=text, out=False),
    )


class AdoptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.session = Path(self.directory.name) / "fixture.session"

    def parse(self, *argv):
        return cli.build_parser().parse_args(cli.normalize_globals([
            "--session", str(self.session), *argv,
        ]))

    async def inbox(self, count, *, limit=5000, scan=5000, unread=False, zero=False,
                    mixed=False, text="fixture", failure=None, legacy=False):
        rows = [dialog(i + 1, unread=not zero and i == count - 1, text=text,
                       kind=("user", "channel", "chat")[i % 3] if mixed else "user")
                for i in range(count)]
        calls = []

        async def iterate(**kwargs):
            calls.append(kwargs)
            for index, row in enumerate(rows[:kwargs["limit"]]):
                # Model multiple provider pages without adding another transport.
                if index % 100 == 0:
                    await asyncio.sleep(0)
                yield row
            if failure is not None:
                raise failure

        client = SimpleNamespace(iter_dialogs=iterate, disconnect=AsyncMock(),
                                 send_read_acknowledge=AsyncMock())
        flags = ["inbox", "--limit", str(limit)]
        if not legacy:
            flags += ["--scan-limit", str(scan)]
        if unread:
            flags += ["--unread", "--dm-only"]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(b, "resolve_api_credentials", return_value=(123, "fixture")), \
                patch.object(b, "connect_client", AsyncMock(return_value=(client, None))), \
                redirect_stdout(out), redirect_stderr(err):
            code = await b.run(self.parse(*flags))
        client.disconnect.assert_awaited_once()
        client.send_read_acknowledge.assert_not_awaited()
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue(), calls

    async def test_late_unread_and_more_than_200_results(self):
        _, result, _, calls = await self.inbox(3201, unread=True)
        self.assertEqual(result["scanned_count"], 3201)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(result["dialogs"][0]["chat_id"], 3201)
        self.assertTrue(result["exhausted"])
        self.assertEqual(calls, [{"limit": 5000, "ignore_pinned": False}])
        _, result, _, _ = await self.inbox(3201)
        self.assertEqual(result["returned_count"], 3201)
        self.assertLessEqual(datetime.fromisoformat(result["observed_start"]),
                             datetime.fromisoformat(result["observed_end"]))
        self.assertEqual(datetime.fromisoformat(result["observed_start"]).utcoffset().total_seconds(), 0)

    async def test_stop_reasons_never_claim_completeness_at_budget(self):
        for count, scan, limit, reason, scanned in (
            (0, 10, 10, "exhausted", 0), (9, 10, 10, "exhausted", 9),
            (10, 10, 10, "scan_limit", 10), (11, 10, 10, "scan_limit", 10),
            (9, 10, 3, "result_limit", 4), (3, 10, 3, "exhausted", 3),
        ):
            with self.subTest(count=count, scan=scan, limit=limit):
                code, result, err, _ = await self.inbox(count, scan=scan, limit=limit)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(result["stop_reason"], reason)
                self.assertEqual(result["scanned_count"], scanned)
                self.assertEqual(result["exhausted"], reason == "exhausted")
                self.assertLessEqual(result["returned_count"], limit)
                self.assertLessEqual(scanned, scan)

    async def test_zero_unread_matches_and_legacy_shape(self):
        for count, complete in ((9, True), (10, False)):
            _, result, _, _ = await self.inbox(count, scan=10, unread=True, zero=True)
            self.assertEqual(result["dialogs"], [])
            self.assertEqual(result["exhausted"], complete)
        _, result, _, _ = await self.inbox(30, limit=20, legacy=True)
        self.assertEqual(set(result), {"dialogs"})
        self.assertEqual(len(result["dialogs"]), 20)
        self.assertNotIn("peer_id", result["dialogs"][0])

    async def test_peer_ids_are_unique_and_chat_ids_reusable(self):
        _, result, _, _ = await self.inbox(303, mixed=True)
        self.assertEqual([r["peer_id"] for r in result["dialogs"][:3]],
                         ["user:1", "channel:2", "chat:3"])
        self.assertEqual([r["chat_id"] for r in result["dialogs"][:3]],
                         [1, -1000000000002, -3])
        self.assertTrue(result["dialogs"][0]["pinned"])
        self.assertTrue(result["dialogs"][1]["archived"])
        same_id = [dialog(123, kind=k) for k in ("user", "chat", "channel")]
        self.assertEqual([b.build_dialog_payload(d)["chat_id"] for d in same_id],
                         [123, -123, -1000000000123])

    async def test_output_limit_counts_ascii_json_without_truncating_text(self):
        text = "界" * 200
        _, result, _, _ = await self.inbox(5000, text=text)
        self.assertEqual(result["stop_reason"], "output_limit")
        self.assertFalse(result["exhausted"])
        self.assertLess(result["returned_count"], 5000)
        self.assertLess(len(json.dumps(result, ensure_ascii=True).encode()) + 1, 4 * 1024 * 1024)
        self.assertEqual(result["dialogs"][-1]["last_message"]["text"], text)

    async def test_invalid_budget_precedes_credentials_and_lock_creation(self):
        for flags in (("--scan-limit", "0"), ("--scan-limit", "5001"),
                      ("--scan-limit", "10", "--limit", "5001"), ("--limit", "201")):
            with self.subTest(flags=flags), patch.object(b, "resolve_api_credentials") as creds, \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(await b.run(self.parse("inbox", *flags)), 1)
                creds.assert_not_called()
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    async def test_partial_transport_failure_never_emits_coverage(self):
        for failure in (TimeoutError("fixture"), RuntimeError("fixture")):
            code, result, error, _ = await self.inbox(1, failure=failure)
            self.assertEqual(code, 1)
            self.assertIsNone(result)
            self.assertIn("error", json.loads(error))
        with self.assertRaises(asyncio.CancelledError):
            await self.inbox(1, failure=asyncio.CancelledError())

    async def test_shared_locks_validated_before_any_runtime_io(self):
        with patch.object(b, "resolve_api_credentials") as creds, redirect_stderr(io.StringIO()):
            args = self.parse("status", "--shared-lock", "relative")
            self.assertEqual(await b.run(args), 1)
            creds.assert_not_called()
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    async def test_shared_machine_and_legacy_locks_exclude_separate_process(self):
        if b.fcntl is None:
            self.skipTest("POSIX process-lock proof")
        shared = [str(Path(self.directory.name) / n) for n in ("machine.lock", "legacy.lock")]
        # Simulate either legacy protocol independently, using only temp locks.
        holder = ("import fcntl,sys; f=open(sys.argv[1], 'a+'); "
                  "fcntl.flock(f, fcntl.LOCK_EX); print('ready', flush=True); sys.stdin.read()")
        for held in shared:
            process = subprocess.Popen([sys.executable, "-c", holder, held],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                # communicate is bounded below; readline follows the child's one-line handshake.
                self.assertEqual(process.stdout.readline().strip(), "ready")
                args = self.parse("read", "--chat", "123", "--rpc-timeout", "0.03",
                                  "--shared-lock", shared[0], "--shared-lock", shared[1])
                with patch.object(b, "resolve_api_credentials", return_value=(123, "fixture")), \
                        patch.object(b, "connect_client") as connect, redirect_stderr(io.StringIO()):
                    self.assertEqual(await cli.invoke(args), 1)
                    connect.assert_not_called()
            finally:
                process.communicate("", timeout=5)
            async with b.acquire_session_lock_async(self.session, timeout_seconds=0, shared_locks=shared):
                pass
        # All standalone operations keep their original lock too.
        paths = b.resolve_session_lock_paths(self.session, shared_locks=shared)
        self.assertEqual(paths, sorted({Path(str(self.session.resolve()) + ".lock"),
                                       *(Path(p).resolve() for p in shared)}, key=str))

    async def test_mark_read_and_unread_keep_distinct_provider_semantics(self):
        client = SimpleNamespace(send_read_acknowledge=AsyncMock(), disconnect=AsyncMock(),
                                 __call__=AsyncMock())
        request_client = AsyncMock()
        request_client.disconnect = AsyncMock()
        with patch.object(b, "resolve_api_credentials", return_value=(123, "fixture")), \
                patch.object(b, "connect_client", AsyncMock(return_value=(client, None))), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(await b.run(self.parse("mark-read", "--chat", "123")), 0)
        client.send_read_acknowledge.assert_awaited_once_with(123)
        with patch.object(b, "resolve_api_credentials", return_value=(123, "fixture")), \
                patch.object(b, "connect_client", AsyncMock(return_value=(request_client, None))), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(await b.run(self.parse("mark-unread", "--chat", "123")), 0)
        request = request_client.await_args.args[0]
        self.assertTrue(request.unread)
        self.assertEqual(request.peer, 123)
        request_client.send_read_acknowledge.assert_not_awaited()

    async def test_compact_json_and_text_match_existing_read_contract(self):
        message = SimpleNamespace(id=123, chat_id=123, message="  exact\n text ",
                                  date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                  reply_to=SimpleNamespace(reply_to_msg_id=9, reply_to_top_id=8),
                                  sender_id=456, out=False)
        client = SimpleNamespace(get_messages=AsyncMock(return_value=[message]),
                                 send_read_acknowledge=AsyncMock(), disconnect=AsyncMock())
        for json_mode in (False, True):
            out = io.StringIO()
            flags = ["read", "--chat", "123", "--format", "compact"]
            if json_mode:
                flags += ["--json"]
            with patch.object(b, "resolve_api_credentials", return_value=(123, "fixture")), \
                    patch.object(b, "connect_client", AsyncMock(return_value=(client, None))), redirect_stdout(out):
                self.assertEqual(await cli.dispatch(self.parse(*flags)), 0)
            if json_mode:
                result = json.loads(out.getvalue())
                self.assertEqual(result["paging"], {"newer_after_id": 123, "older_before_id": 123})
                self.assertEqual(result["messages"][0], {
                    "buttons": [], "date": "2026-01-01T00:00:00+00:00", "dir": "in", "id": 123,
                    "media": None, "reply_to": 9, "sender": 456, "text": "exact text", "top": 8, "topic": None,
                })
            else:
                self.assertIn('id=123 date=2026-01-01T00:00:00+00:00 dir=in sender=456 reply_to=9 top=8 topic=- media=- buttons=[] text="exact text"', out.getvalue())
                self.assertIn("Paging: newer --after-id 123 | older --before-id 123", out.getvalue())
        client.send_read_acknowledge.assert_not_awaited()


class SelectionTests(unittest.TestCase):
    def test_explicit_then_standalone_env_then_xdg_no_foreign_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            with patch.dict(os.environ, {"XDG_DATA_HOME": directory,
                    "TELEGRAM_USER_SESSION": str(base / "selected.session"),
                    "USERBOT_SESSION": str(base / "foreign.session")}, clear=True):
                self.assertEqual(b.resolve_session_path(str(base / "explicit.session")), base / "explicit.session")
                self.assertEqual(b.resolve_session_path(None), base / "selected.session")
                del os.environ["TELEGRAM_USER_SESSION"]
                self.assertEqual(b.resolve_session_path(None), base / "telegram-user" / "user.session")
                with self.assertRaisesRegex(ValueError, "absolute"):
                    b.resolve_session_path("relative.session")
            self.assertEqual(list(base.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
