# Provenance

`backend.py` and `compat.py` are derived from `scripts/telegram-e2e/telethon_cli.py`
and `telethon_compat.py` in Artem's local OpenClaw fork. The source checkout's HEAD
at extraction was `896b885ac350451c6548de81085db42732bf2f24`.

The source files were MIT licensed with the Peter Steinberger copyright notice
retained in this repository's LICENSE. This extraction does not claim authorship
of Telethon or the OpenClaw project.

Standalone changes remove gateway/session-owner discovery, add independent account
storage, preserve login persistence, enforce exact input, and supply packaging,
operator UX, docs, agent instructions, and offline tests.

Telethon is installed as a dependency, not vendored. Its own license and authorship
remain with its maintainers.
