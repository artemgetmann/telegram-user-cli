# Architecture

`telegram-user` enters `cli.py`, which loads explicitly selected credentials,
parses options, handles terminal login, and bounds calls. `backend.py` owns
Telegram requests, normalization, pending login state, and session locks.
`compat.py` owns SQLite session compatibility and Telethon construction.

Dependencies flow from CLI to backend to compatibility helper to Telethon.
No module may import OpenClaw or Jarvis. Importing the package does not connect
to Telegram or open sessions. Tests enforce this boundary.

JSON success records go to stdout. Errors go to stderr with a nonzero exit.
Credentials and session state live outside the repository. An uncertain mutation
is never retried automatically. See contracts/session.md for persistence rules.

The extracted backend is deliberately larger than the 500-line review trigger:
it preserves proven protocol operations instead of splitting them during release.
The maintainer owns this exception; reconsider when adding new transport features.
Behavior tests cover text fidelity, auth persistence, filtering, and locking.

Local tests prove fixture behavior. A built and installed wheel proves packaging.
Live reads prove account connectivity; only a live send and readback prove delivery.
Publishing source and publishing social posts are separate external actions.
