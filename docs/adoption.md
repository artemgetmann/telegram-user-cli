# Standalone adoption: local candidate

This candidate targets the existing standalone CLI. It is not installed or released.
It ports the bounded inbox contract reviewed in OpenClaw PR #1479, source head
`6f5ba6f0a4aae2891edb4c07aae4e10ae3ec35b4`, without changing that checkout or PR.
The retained OpenClaw diff fingerprint was
`a51262c31496c01d887c40f999f5dbd7979cf573e238c7849fd9e29f4afac156`.

## Bounded checklist

| Item and source | What good looks like | Offline proof |
| --- | --- | --- |
| `backend.py`: inbox parser/validation/iteration | Legacy JSON and 200-result limit remain; opt-in `--scan-limit` and result limit accept 1..5000; result lookahead counts against scan budget | `test_adoption`: late unread at 3201, >200 rows, all stop reasons, empty/zero-match/exact-boundary cases, invalid budgets before runtime I/O |
| `backend.py`: coverage envelope | `exhausted=true` only after provider exhaustion; report scanned/returned counts, limits, UTC observation interval, stop reason; ASCII JSON fits 4 MiB | Output-cap test with Unicode text; errors/cancellation emit no successful coverage |
| `backend.py`: dialog identities | `chat_id` stays Telethon's marked ID reusable by read/send; opt-in `peer_id` is `user:123`, `chat:123`, or `channel:123` using bare ID | Mixed peers, same bare ID across namespaces, pinned/archived metadata |
| `backend.py`: explicit account and locks | Absolute session selection only; no foreign credential/session discovery; shared locks are explicit, sorted, deduplicated; standalone session lock retained | Selection precedence, invalid lock before I/O, separate-process exclusion for both simulated machine and legacy locks, cancellation/reacquisition |
| `cli.py`: compact read | Existing `read --format compact` text and `--json` projection accepted; default remains raw JSON; reads send no acknowledgement | Compact field/paging assertions plus existing exact-text and read-state tests |
| Existing mutations | Send acknowledgement, uncertainty/no retry, mark-read, mark-unread stay distinct | Existing send/timeout/ack tests plus provider-request assertions |
| Independent review | Final source/tests/docs reviewed with no unresolved actionable findings | See local implementation handback; this is not live-account acceptance |

## Deliberate compatibility boundaries

- The existing standalone Python package remains the only transport. No gateway,
  monitor service, runtime discovery, or OpenClaw dependency is added.
- JSON remains default. Compact text is an explicit `read --format compact` view;
  append `--json` for the compact JSON projection. Compact text normalizes whitespace;
  raw reads retain the original message text.
- `doctor` remains an offline setup check. `auth_unverified` is not a broken session;
  only an authorized live `status` can prove authentication.
- `wait` keeps standalone flags (`--topic-anchor`, `--timeout` and `--poll-interval`
  in seconds), required `--after-id`, and its existing `matched`/`timed_out` JSON.
  It does not emulate the fork's `--thread-anchor`, millisecond flags, or result schema.
  Callers depending on those contracts need explicit translation before cutover.
- Message/caption files and stdin, replies, downloads, and topic commands retain
  their standalone contracts. Generic transcription and durable monitoring stay outside.
- `--session` or `TELEGRAM_USER_SESSION` wins over XDG storage. Relative selectors
  are now rejected instead of being resolved against an arbitrary working directory.
  `USERBOT_SESSION` and OpenClaw runtime variables are not automatically adopted.
- `--env-file` loads only the named file; current process environment still wins.
  The cutover must not silently mix credentials from one account with another session.
- Pending-login suffixes still differ (`.login.json` versus `.openclaw-login.json`).
  This candidate does not migrate pending authentication or enable mixed login/logout.
- Shared locks are a generic explicit bridge, not automatic protection against
  uncooperative clients. Every mixed-version caller must share at least one identical
  lock file. Supply both the confirmed machine and legacy lock paths when the old
  clients can use either protocol. Existing standalone callers keep their session lock.

## Verification and later rollout gates

1. **Release validation gate.** Local verification uses a temporary environment with
   pinned `requirements-dev.txt` dependencies: `python -m unittest discover -s tests -v`,
   `ruff check src tests`, and `python -m build --no-isolation`. Build and test the wheel
   outside either checkout; keep logs and artifact hashes in the local verification
   handback. This authorizes temporary test installations only. A release candidate
   should use an immutable version (suggest next minor `0.2.0` for the relative-path
   contract change); tagging, pushing, publication, and active-runtime installation
   remain outside this local authorization.
2. **Account-state inspection gate.** Obtain explicit authorization to identify the
   existing credential selector, session, pending-auth status, schema/version, and
   active lock protocols. Record selectors privately, never credential/session content.
   Do not copy, move, create, reauthenticate, or normalize the real session. Inspect
   compatibility without constructing a client. Stop for a pending login, ambiguous
   account ownership, unsupported schema, unknown lock owner, or any schema/permission
   change required to use the canonical session. Those changes need separate explicit
   authorization and a preservation plan; do not trigger them by probing `status`.
3. **Cutover gate.** Obtain authorization naming the exact installation, caller configs,
   account/session, credential source, and all shared lock paths. Inventory actual sweep
   commands and consumers; check flags, output schema, timeout handling, and zero-match
   handling against the checklist. Preserve old executable/config selections for rollback.
   Avoid overlapping live sweeps; stopping/restarting any service needs explicit scope.
4. **Live Telegram-read acceptance gate.** Obtain authorization for one bounded sweep,
   its timing, filters, maximum scan/result counts, and selected account. Use explicit
   selectors and both shared-lock paths, e.g. `telegram-user --env-file CREDENTIAL_FILE
   --session EXISTING_SESSION --shared-lock MACHINE_LOCK --shared-lock LEGACY_LOCK inbox
   --unread --scan-limit 5000 --limit 5000 --json` (placeholders, not runnable account paths).
   Before any `status` or inbox/read invocation, explicitly authorize the required
   canonical local-state access too. Existing `connect_client` uses a persistent
   Telethon session: it can update caches/schema, create directories, and chmod the
   database; `status` can clear a pending-auth sidecar. Shared locks serialize these
   effects and do not make the filesystem read-only. Preflight must show that auth,
   schema, and permissions can be preserved; stop otherwise instead of normalizing.
   Permission/schema changes are excluded unless separately authorized. Record the
   allowed local cache writes and verify logical auth/read-state preservation; do not
   promise byte-for-byte SQLite immutability. Only after these gates, verify expected
   identity with authorized `status`. Read back only specifically authorized chats.
   No send, mark-read, mark-unread, login/logout, or URL-button action.
5. **Acceptance criteria.** Confirm the same intended account/session and unchanged
   auth/read-state; the consumer receives the full JSON record within its output budget;
   late known unread chats are present; zero matches never imply completeness unless
   `exhausted=true`; all truncated scopes visibly report scan/result/output limits.
   `exhausted` proves enumeration within an observation interval, not an atomic snapshot
   or a guarantee that another message did not arrive afterward. If coverage is bounded,
   report it as partial; do not automatically expand scope or rerun indefinitely.
6. **Rollback gate.** On acceptance failure, stop using the candidate; with approved caller
   configuration authority restore recorded executable/config selectors. Reuse the same
   session and lock identities; do not delete lock files, roll back SQLite bytes, log out,
   or copy an older session. Uncertain mutations require targeted reconciliation first.
   Keep PR #1479 and the old installation available until replacement acceptance is
   verified; closing the PR or removing the old install needs later authorization.

## Local evidence and remaining gates

The final source verification passed 61 offline tests on Python 3.13 with the exact
Telethon 1.43.2 / python-dotenv 1.2.1 pins and Ruff 0.14.0. Dependencies were installed
from official PyPI only into a temporary verification environment under the later
explicit test-install authorization. Build/wheel verification logs and hashes belong
to the local verification handback; no source publication or active installation follows
from those checks. Earlier compatibility proof also passed on Telethon 1.43.1.
No live account, active installed CLI, caller configuration, running service, real lock,
or real account state was accessed or changed. Live acceptance remains unexecuted.
