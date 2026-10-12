---
name: telegram-user-cli
description: Use the standalone telegram-user CLI to read, send, reply, or wait through an account owner's real Telegram account, including DMs and forum topics. This uses a user session rather than a bot token.
---

# Telegram User CLI

Use `telegram-user` for the account owner's authorized Telegram operations.
It requires an existing installation and owner-provided API credentials and login.
Read `telegram-user --help` and the relevant command's help if syntax is uncertain.

## Workflow

1. Run `telegram-user doctor` for local setup. `status` verifies actual authentication.
2. If the exact chat is unknown, discover it with `inbox`. Never guess a recipient.
3. Read the exact chat with `read --chat TARGET --limit 5` when context is requested
   or immediately before an authorized reply. Preparing a draft alone does not
   authorize browsing another person's conversations.
4. Preserve the exact approved message in a UTF-8 file with the runtime's native
   file-writing tool. Call `send --chat TARGET --message-file ABSOLUTE_PATH`.
   If the runtime can supply atomic stdin, `--message-file -` also works.
5. Send only within the account owner's explicit recipient, wording, and timing
   authorization. Changes to the draft require renewed approval unless already authorized.
6. Inspect the send result and read back its `message.message_id` in the exact chat.
   A submitted request is not proof that the intended message appears there.

Use `--reply-to MESSAGE_ID` for a reply. For forum topics, resolve an exact title
with `topic-resolve`, then use its `topic_anchor` with read/send. Duplicate titles
are ambiguous; do not select one from neighboring chat messages.

## Useful commands

```sh
telegram-user inbox --unread --dm-only --limit 10
telegram-user read --chat @username --limit 5
telegram-user send --chat @username --message-file /private/path/message.txt
telegram-user send --chat @username --reply-to 123 --message-file /private/path/reply.txt
telegram-user wait --chat @username --after-id 123 --timeout 60
```

Apply `--env-file` or `--session` only when the owner supplied that exact configuration.
Never discover or copy another application's session automatically. Never expose API
hashes, login codes, 2FA passwords, session bytes, or private chats in logs or public output.

`wait` reads only. It does not authorize an automatic reply or a background monitor.
Topic deletion, unread-state changes, joining via URL buttons, and uploads are external
actions and need owner authorization for the exact scope.

## Errors and partial success

Success is JSON on stdout; errors are JSON on stderr with nonzero exit status.
If a mutation times out, disconnects, or reports a send acknowledgement failure,
it may already have succeeded. Read the exact target before considering a retry.
Never automatically resend. Report delivery and dialog read-state as separate outcomes
when one succeeds and the other fails.
