# Session and mutation contract

- Only backend.py and compat.py may open Telegram session state.
- Each selected session has a stable lock. Independent processes serialize access.
- Fresh login state must survive disconnect and the next CLI invocation.
- Credentials are never copied from another application automatically.
- New credential files are private. User-selected parent directories are not chmodded.
- Reading messages does not acknowledge them as read. `mark-read` is explicit;
  sending acknowledges the dialog after delivery and reports acknowledgement failure.
- A timeout or error after mutation may mean Telegram accepted it. Read the exact
  target before deciding whether another mutation is needed.
- Logout clears only the exact selected local session artifacts. It does not
  promise to revoke every Telegram device or remote authorization.
- Tests use isolated files and fake clients. No tests contact real recipients.
