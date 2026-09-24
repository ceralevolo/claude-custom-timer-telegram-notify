# claude-custom-timer-telegram-notify

A Claude Code plugin that sends you a private Telegram message when Claude finishes a turn.
It is designed for long tasks: you switch it on per session, and you can set a **minimum turn
duration** so the quick back-and-forth at the start of a session doesn't spam you.

```text
/notify                 show the current state
/notify on              notify at the end of every turn
/notify on min 30       notify only turns that last at least 30 s (also 45s, 5m, 1h, min=2m)
/notify off             stop notifying in this session
```

`/notify` never reaches the model: a `UserPromptExpansion` hook intercepts it, stores the switch
and blocks the expansion. No model turn, no tokens and no notification are spent on it.

## What gets sent

| Icon | When |
|---|---|
| ✅ / ❓ | `Stop`: end of turn. ❓ is used when the last reply looks like a question. |
| ⚠️ | `StopFailure`: the turn ended on an API error. |
| ⏸️ | `Notification`: Claude is blocked on you (permission prompt, input request). |

Each message carries the working directory's basename, the first 8 characters of the session id
and the last assistant message, truncated to 3900 characters. The notifier skips intermediate
turns (active background tasks or scheduled loops) and non-blocking notifications such as
`idle_prompt`. It deduplicates repeated hook invocations.

The switch and threshold apply to all three kinds. The turn clock starts on `UserPromptSubmit`,
or on the expansion of any other slash command. If the start of a turn is unknown, the notifier
sends anyway (fail-open).

## Install

```text
/plugin marketplace add ceralevolo/claude-custom-timer-telegram-notify
/plugin install telegram-notify@ceralevolo-plugins
```

Requirements: `python3` (stdlib only) and a Telegram bot.

### Credentials

Create a bot with @BotFather, send it a message, and read your chat id from
`https://api.telegram.org/bot<TOKEN>/getUpdates`. Then store both in a private file. Never commit
this file or paste it anywhere:

```bash
mkdir -p -m 700 ~/.config/claude-telegram-notify
install -m 600 /dev/null ~/.config/claude-telegram-notify/credentials.env
$EDITOR ~/.config/claude-telegram-notify/credentials.env
```

```text
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```

The file must be a regular file (no symlink), owned by you, and have no group or other
permission bits. Otherwise nothing is sent. To keep it elsewhere, set
`CLAUDE_TELEGRAM_NOTIFY_CREDENTIALS` in the `env` block of `~/.claude/settings.json`.

### Default for new sessions

Notifications are **off** by default. To change that, put one line in
`~/.config/claude-telegram-notify/default` using the same syntax as the command arguments, for
example `on min 60`. A missing or unreadable file means off.

## Files and privacy

- Session switches live in `$CLAUDE_PLUGIN_DATA/sessions/`, or
  `~/.local/state/claude-telegram-notify/sessions/` outside a plugin. Each file is named by a
  digest of the session id and holds only `enabled`, `min_seconds`, `turn_started_at` and
  `updated_at`. Files older than 7 days are pruned.
- The deduplication database, `events.sqlite3` in the same directory, stores only SHA-256
  digests, a status and a timestamp.
- Prompts, transcripts, tool I/O and `error_details` are never read or sent. `transcript_path`
  is only `stat()`-ed for deduplication.
- On a failure the hooks exit 0 and never block Claude. The one intentional exception is
  `/notify`, which exits 2 to block its own expansion.

## Environment overrides

| Variable | Purpose |
|---|---|
| `CLAUDE_TELEGRAM_NOTIFY_CREDENTIALS` | credentials file path |
| `CLAUDE_TELEGRAM_NOTIFY_DEFAULT` | default-switch file path |
| `CLAUDE_TELEGRAM_NOTIFY_STATE` | deduplication database path |
| `CLAUDE_TELEGRAM_NOTIFY_SESSIONS` | session switch directory |
| `CLAUDE_TELEGRAM_NOTIFY_API_BASE` | Telegram API base URL (tests) |

## Tests

```bash
python3 -m unittest discover -s plugins/telegram-notify/tests -v
```

The suite runs the real script as a subprocess against a local HTTP server and never contacts
Telegram.

## Uninstall

`/plugin uninstall telegram-notify@ceralevolo-plugins`. Remove
`~/.config/claude-telegram-notify/` too if you no longer need the credentials.
