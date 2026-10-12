# Contributor instructions

- Read VISION.md for scope and docs/architecture/overview.md for ownership.
- Keep the CLI independent of OpenClaw, Jarvis, gateways, and user machine paths.
- Never commit credentials, Telegram sessions, private chats, or real account fixtures.
- Run `python -m unittest discover -s tests -v` and `ruff check src tests`.
- Tests must stay offline with temporary sessions and fake transport clients.
- Keep external mutations explicit. Never retry an uncertain send automatically.
- Use Conventional Commits with What, Why, and Risk bullets (see .gitmessage).
