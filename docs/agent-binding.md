# One binding for agent calls

`telegram-user-bound inbox --unread --limit 200 --scan-limit 3000 --json` is an optional
short form for agents using one operator-selected account. The existing `telegram-user`
interface is unchanged. This addition is source-only: no binding file, installation,
skill, running service, account state or deployed caller is changed automatically.

The wrapper has no named profiles, account discovery, auth management, daemon, or new
transport. It validates one non-secret JSON file and command, then `exec`s the same
Python interpreter with an explicit selected package root, pinned selectors, and the original
argument tokens. The exec bootstrap replaces Python's implicit working-directory search
entry with the loaded binding's package root; it does not rewrite `PYTHONPATH` or
other environment values. A package in the caller's directory cannot replace the selected
CLI. It reads no stdin. PID, descriptors, signals, exit status and caller
deadline belong to the existing process; no supervisor, shell or retry is added.

## Operator configuration (not agent-supplied on each call)

Choose `$TELEGRAM_USER_BINDING` as an absolute config path, or use
`$XDG_CONFIG_HOME/telegram-user/binding.json` (default `~/.config/telegram-user/binding.json`).
The file is operator-controlled; anyone able to rewrite it can select another binding.
It must contain exactly these fields, with absolute paths and an explicit lock list:

```json
{
  "version": 1,
  "env_file": "/private/example/account.env",
  "session": "/private/example/account.session",
  "shared_locks": ["/private/example/machine.lock", "/private/example/legacy.lock"]
}
```

These are synthetic examples, not discoverable account defaults. Never store API
credentials, OTPs, passwords, session data, or message text in this config. Deployment
must use the complete existing binding, including every required shared lock. A missing
field is an error; an explicitly empty lock list requires an independently approved
retirement of all shared-lock clients. Duplicate/unknown keys, unsupported versions,
relative selectors, control characters, a session missing its `.session` suffix, and
configs larger than 16 KiB fail before the transport starts. No config is auto-created.

`telegram-user-bound --help` explains the schema without a binding. `--check-binding`
validates and prints only these non-secret selectors; it does not open their targets
or prove credentials/authentication/session readiness. Missing/invalid config returns
JSON `E_BINDING_CONFIG` on stderr and exit 2. No fallback account is selected.

The normal command parser rejects caller `--env-file`, `--session`, `--lock`, and
`--shared-lock` selections, including parsed abbreviations. Other invalid commands
fail as `E_USAGE` without echoing payloads. Use the unbound tool for a separately
authorized alternate selection. Literal message values that resemble options remain
payloads; supported `--` separators and all original tokens are forwarded unchanged.
The underlying CLI's own syntax/input validations still apply.

Child `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are removed so the selected explicit
env file remains authoritative. No credential values are printed/copied and the parent
environment is unchanged. Every other environment value is preserved, including all
six existing runtime binding variables. Explicit primary `--lock SESSION.lock` and
the original shared-lock list override ambient session/lock selectors. The existing
backend still acquires its canonical per-session lock and every supplied shared lock.

## Proof and ergonomic acceptance

Tests use only synthetic config, env files, paths and fake transports. They check
argument spaces/Unicode/option-like payloads/`--`, parsed override rejection, child
environment isolation, the complete lock set, failure JSON, config-only discovery,
PID/exit/stdin forwarding, caller timeout termination, and original CLI regression tests.
No claim of reduced network or wall-clock latency is made. Parser/import work can add
startup cost; this design reduces repeated configuration, not transport work.

Compare the same command tail before/after deployment: repeated env-file/session/two
shared-lock selectors fall from four occurrences to zero. The agent supplies only the
selected executable and task arguments; an operator supplies the same four path facts
once in the binding. Actual command-length measurements belong in the private task
handback, since deployment-specific paths do not belong in generic project defaults.

## Eventual tool-owned state: decision and gates

Prefer eventual `$XDG_DATA_HOME/telegram-user/` (default `~/.local/share/telegram-user/`)
for this tool's state. That is already its unbound default. Moving a lock into another
application's directory does not establish independent ownership. Keep the existing
session/credential/lock binding unchanged for the first short-command deployment.
Migration is a separate approval and does not follow from this source change.

1. **Inventory before choosing a cutover.** Enumerate every configured caller: direct
   CLI/skills, sweep prompt, bundled backends, launchd/systemd monitor definitions,
   detached processes, legacy scripts, and rollback entrypoints. Record configured
   versus actually running separately. A configured service is not proof of activity;
   an unobserved process is not proof of absence. Identify selector/credential precedence,
   executable version, account identity proof, and exact lock file identities for each.
   Existing old/current session paths must not be assumed to represent the same account.
2. **Prove the short binding first.** Separately approve installation and creation of
   the non-secret binding using existing path references. Validate config offline, then
   approve one narrow live comparison under existing timing/source rules. Keep both
   legacy shared locks and the mandatory per-session lock. Do not change services,
   migrate auth, or replace sweep logic in this step. Save exact caller rollback selections.
3. **Migration approval must name all writers and data.** Choose one canonical tool-owned
   destination, confirm the intended account without revealing its ID/key in public
   receipts, and obtain explicit authority for service/process quiescence, consistent
   private backup/transfer, binding/caller changes, local filesystem effects, and bounded
   acceptance. No new login, credential duplication, permission change, or expanded
   account access is implicit. If identity or writer ownership is uncertain, stop.
4. **Quiesce and fence.** Stop every confirmed writer under that authority; verify rather
   than assuming a timeout killed it. Hold all existing cooperative lock identities in
   the same order throughout transition. Preserve lock inodes: no deletion, rename,
   replacement, or symlink retarget. Old callers that cannot be fenced must remain
   disabled; moving a database while they still address the old copy creates split brain.
5. **Consistent private backup/transfer.** Do not copy an active SQLite file alone or
   ignore WAL/journal state. With writers quiesced and locks held, use an approved
   SQLite backup/transactional transfer procedure; validate integrity and expected
   schema/version without exposing auth rows. Restrict backup access under explicit
   authority and define retention. Preserve credentials in an approved secret source;
   use path references, not values in config/code/logs. No copying occurs in this task.
6. **Interrupted cutover is fail-closed.** Define a small durable operator transaction
   receipt with old/new paths, lock identities and phase, never secrets. Before resuming,
   reconcile the actual phase and writer ownership. Prepare the destination privately,
   validate it, then atomically publish the selected binding while all writers remain
   fenced. Sequential caller updates are safe only under that fence. Do not run two
   independently mutable sessions or infer completion from a partially changed config.
7. **Acceptance and rollback.** Under explicit account-read/timing approval, verify
   intended identity privately and both mixed/unread bounded commands with full JSON,
   counts/limits/observation times and truthful stop reasons. Reads must not acknowledge
   messages. Check logical auth/read-state, no duplicate writer, and all caller readbacks.
   A later normal scheduled run is separate from manual smoke proof. On failure, keep
   writers stopped while restoring saved selectors. Before any destination write, the
   untouched old session may be restored as owner; after writes, do not blindly restore
   a stale backup—choose/reconcile one authoritative state under the same lock fence.
8. **Retire legacy locks last.** Require evidence that every configured and running
   client has transitioned or is permanently disabled, no old-version restart route
   survives, rollback no longer needs those locks, and the intended account remains
   correct. Retirement needs separate approval; keep inode coordination until then.

## Minimal synthetic rehearsal

Use two temporary SQLite fixtures with intentionally different synthetic identities,
a WAL-mode fixture, two legacy locks and one per-session lock. A fake old writer and
new reader must block each other through the shared lock set. Show that an unfenced
old writer makes migration refuse; quiesce it, create/verify a consistent fixture
backup, and switch a temporary binding atomically. Inject interruption before/after
destination preparation, binding publication, and caller updates. Reconcile each
phase with all writers fenced; demonstrate rollback before writes and refusal of a
stale rollback after destination writes. Verify the old lock inodes survive and a
configured-but-stopped fake service is reported distinctly. This rehearsal is a plan,
not a migration implementation or an already-passed live test.
