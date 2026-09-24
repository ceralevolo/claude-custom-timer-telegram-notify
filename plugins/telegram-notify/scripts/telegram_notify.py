#!/usr/bin/env python3
"""Claude Code hook (Stop/StopFailure/Notification) → private Telegram message, fail-open.

Reads the JSON payload from stdin only. Never reads the transcript: only the
stat() metadata of `transcript_path` is used for deduplication. The SQLite
state holds only digests, a status and timestamps.

Per-session switch: `/notify on [min <duration>]`, `/notify off` and `/notify`
(status) are intercepted on UserPromptExpansion (or on UserPromptSubmit when
typed as plain text) and exit 2: the prompt is blocked, the stderr message is
shown to the user only and no model turn runs. UserPromptSubmit and other
slash commands record the turn start used by the minimum-duration threshold.
Every other path exits 0.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CONFIG_DIR = Path.home() / ".config/claude-telegram-notify"
DEFAULT_CREDENTIALS = CONFIG_DIR / "credentials.env"
DEFAULT_SWITCH = CONFIG_DIR / "default"
FALLBACK_DATA_DIR = Path.home() / ".local/state/claude-telegram-notify"
DEFAULT_API_BASE = "https://api.telegram.org"
SESSION_TTL_SECONDS = 7 * 86400
NOTIFY_COMMAND = "notify"
EXIT_BLOCK = 2
USAGE = (
    "Usage: /notify on [min <duration>] · /notify off · /notify (status)\n"
    "Duration: 30, 45s, 5m, 1h — only turns longer than the threshold notify."
)
# `/notify …` typed as text: a plugin command's short name may not resolve as a
# command and then reaches UserPromptSubmit as a plain prompt.
RAW_NOTIFY_PATTERN = re.compile(r"^\s*/(?:[\w-]+:)?notify(?:\s+(.*?))?\s*$", re.DOTALL)
DURATION_PATTERN = re.compile(r"^(\d+)(s|sec|m|min|h)?$")
DURATION_UNITS = {None: 1, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600}
SWITCH_OFF = {"enabled": False, "min_seconds": 0}
MAX_MESSAGE_LENGTH = 3900
TRUNCATION_MARKER = "\n\n[message truncated]"
STAT_SENTINEL = "-"
ACTIVE_TASK_STATUSES = {"running", "pending", "queued", "starting", "in_progress"}
BLOCKING_NOTIFICATION_TYPES = {
    "permission_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
}
# Explicit requests for a decision or input, in English and Italian.
QUESTION_PATTERN = re.compile(
    r"\b(let me know|please confirm|do you want|would you like|should i|"
    r"your call|need your|waiting for your|choose|approve|"
    r"mi serve|serve una|ho bisogno di|scegli|confermi|autorizzi|"
    r"decisione|richiesta di input|dimmi se|fammi sapere)\b",
    re.IGNORECASE,
)


def load_credentials(path: Path) -> dict[str, str]:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise PermissionError("credentials file must be a regular file")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("unsafe credentials owner or permissions")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    if not values.get("TELEGRAM_BOT_TOKEN") or not values.get("TELEGRAM_CHAT_ID"):
        raise ValueError("incomplete credentials")
    return values


def work_in_progress(payload: dict[str, object]) -> bool:
    """True for an intermediate turn: active background tasks or scheduled loops.

    Only known active states suppress the notification: on missing or
    unexpected data the notification is sent anyway (fail-open).
    """
    crons = payload.get("session_crons")
    if isinstance(crons, list) and crons:
        return True
    tasks = payload.get("background_tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if (
                isinstance(task, dict)
                and str(task.get("status", "")).lower() in ACTIVE_TASK_STATUSES
            ):
                return True
    return False


class UsageError(ValueError):
    """Invalid /notify arguments: the state is left unchanged."""


def parse_duration(text: str) -> int:
    match = DURATION_PATTERN.match(text)
    if not match:
        raise UsageError(text)
    return int(match.group(1)) * DURATION_UNITS[match.group(2)]


def parse_switch(args: str) -> dict[str, object] | None:
    """`None` = status request; otherwise {'enabled', 'min_seconds'}."""
    tokens = args.lower().replace("=", " ").split()
    if not tokens or tokens == ["status"]:
        return None
    if tokens == ["off"]:
        return dict(SWITCH_OFF)
    if tokens[0] == "on":
        tokens = tokens[1:]
    elif tokens[0] != "min":
        raise UsageError(args)
    if tokens and tokens[0] == "min":
        tokens = tokens[1:]
        if not tokens:
            raise UsageError(args)
    if len(tokens) > 1:
        raise UsageError(args)
    return {"enabled": True, "min_seconds": parse_duration(tokens[0]) if tokens else 0}


def format_duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{v}{u}" for v, u in ((hours, "h"), (minutes, "m"), (secs, "s")) if v]
    return "".join(parts) or "0s"


def _data_dir() -> Path:
    """Persistent state: the plugin data dir when Claude Code provides one."""
    return Path(os.environ.get("CLAUDE_PLUGIN_DATA") or FALLBACK_DATA_DIR)


def _sessions_dir() -> Path:
    return Path(os.environ.get("CLAUDE_TELEGRAM_NOTIFY_SESSIONS") or _data_dir() / "sessions")


def _session_file(session_id: str) -> Path:
    """File name = digest of the session_id: no plaintext id on disk."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _sessions_dir() / f"{digest}.json"


def read_session(session_id: str) -> dict[str, object]:
    try:
        data = json.loads(_session_file(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_session(session_id: str, data: dict[str, object]) -> None:
    directory = _sessions_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, _session_file(session_id))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def prune_sessions(now: float) -> None:
    directory = _sessions_dir()
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        try:
            if path.suffix == ".json" and path.stat().st_mtime < now - SESSION_TTL_SECONDS:
                path.unlink()
        except OSError:
            pass


def load_default() -> dict[str, object]:
    """Default switch for new sessions; missing or unreadable file → OFF."""
    path = Path(os.environ.get("CLAUDE_TELEGRAM_NOTIFY_DEFAULT", DEFAULT_SWITCH))
    try:
        switch = parse_switch(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return dict(SWITCH_OFF)
    return switch or dict(SWITCH_OFF)


def effective_switch(session: dict[str, object]) -> tuple[dict[str, object], bool]:
    """(switch, from_default): the session choice wins over the default."""
    if isinstance(session.get("enabled"), bool) and isinstance(session.get("min_seconds"), int):
        return {"enabled": session["enabled"], "min_seconds": session["min_seconds"]}, False
    return load_default(), True


def describe_switch(switch: dict[str, object], from_default: bool) -> str:
    scope = "(default)" if from_default else "for this session"
    if not switch["enabled"]:
        return f"🔕 Telegram notifications OFF {scope}"
    minimum = int(switch["min_seconds"])
    threshold = f"threshold {format_duration(minimum)}" if minimum else "no threshold"
    return f"🔔 Telegram notifications ON {scope} · {threshold}"


def record_turn_start(session_id: str) -> None:
    now = time.time()
    session = read_session(session_id)
    session["turn_started_at"] = now
    write_session(session_id, session)
    prune_sessions(now)


def handle_notify_command(session_id: str, args: str) -> str:
    try:
        switch = parse_switch(args)
    except UsageError:
        return f"Invalid argument: {args.strip()!r}\n{USAGE}"
    session = read_session(session_id)
    if switch is None:
        current, from_default = effective_switch(session)
        return f"{describe_switch(current, from_default)}\n{USAGE}"
    session.update(switch, updated_at=time.time())
    write_session(session_id, session)
    return describe_switch(switch, False)


def switch_allows(payload: dict[str, object]) -> bool:
    """Session switch and minimum duration; unknown turn start → notify (fail-open)."""
    session = read_session(str(payload.get("session_id") or ""))
    switch, _ = effective_switch(session)
    if not switch["enabled"]:
        return False
    started = session.get("turn_started_at")
    if switch["min_seconds"] and isinstance(started, (int, float)):
        return time.time() - started >= int(switch["min_seconds"])
    return True


def classify(message: str) -> str:
    return "❓" if "?" in message or QUESTION_PATTERN.search(message) else "✅"


def build_message(payload: dict[str, object]) -> str:
    cwd = Path(str(payload.get("cwd") or "/")).name or "/"
    session = str(payload.get("session_id") or "unknown")[:8]
    assistant_message = str(payload.get("last_assistant_message") or "")
    if payload.get("hook_event_name") == "StopFailure":
        icon = "⚠️"
        body = f"API error: {payload.get('error') or 'unknown'}"
        if assistant_message:
            body += f"\n{assistant_message}"
    elif payload.get("hook_event_name") == "Notification":
        icon = "⏸️"
        body = str(
            payload.get("message") or payload.get("title") or "Claude is waiting for your input."
        )
    else:
        body = assistant_message or "Turn completed."
        icon = classify(body)
    text = f"{icon} Claude · {cwd} · {session}\n\n{body}"
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[: MAX_MESSAGE_LENGTH - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return text


def fingerprint(payload: dict[str, object]) -> str:
    """Digest of turn metadata only: no source value is stored."""
    stat_parts = [STAT_SENTINEL] * 4
    transcript_path = payload.get("transcript_path")
    if transcript_path:
        try:
            info = os.stat(str(transcript_path))
            stat_parts = [
                str(info.st_dev),
                str(info.st_ino),
                str(info.st_size),
                str(info.st_mtime_ns),
            ]
        except OSError:
            pass
    parts = [
        str(payload.get("hook_event_name") or ""),
        str(payload.get("session_id") or ""),
        *stat_parts,
        str(payload.get("last_assistant_message") or ""),
        str(payload.get("error") or ""),
        str(payload.get("notification_type") or ""),
        str(payload.get("message") or ""),
    ]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _connect_state(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    connection = sqlite3.connect(path, timeout=5)
    path.chmod(0o600)
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS delivered_event (
            digest TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK(status IN ('sending', 'sent')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()
    return connection


def claim_event(path: Path, digest: str) -> bool:
    with _connect_state(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "INSERT OR IGNORE INTO delivered_event(digest, status) VALUES (?, 'sending')",
            (digest,),
        )
        connection.commit()
        return cursor.rowcount == 1


def mark_sent(path: Path, digest: str) -> None:
    with _connect_state(path) as connection:
        connection.execute(
            "UPDATE delivered_event SET status='sent' WHERE digest=?",
            (digest,),
        )
        connection.commit()


def release_event(path: Path, digest: str) -> None:
    with _connect_state(path) as connection:
        connection.execute(
            "DELETE FROM delivered_event WHERE digest=? AND status='sending'",
            (digest,),
        )
        connection.commit()


def _post_message(payload: dict[str, object], credentials: dict[str, str]) -> None:
    api_base = os.environ.get("CLAUDE_TELEGRAM_NOTIFY_API_BASE", DEFAULT_API_BASE).rstrip("/")
    data = urlencode(
        {
            "chat_id": credentials["TELEGRAM_CHAT_ID"],
            "text": build_message(payload),
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = Request(
        f"{api_base}/bot{credentials['TELEGRAM_BOT_TOKEN']}/sendMessage",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the request")


def deliver(payload: dict[str, object]) -> None:
    credentials_path = Path(
        os.environ.get("CLAUDE_TELEGRAM_NOTIFY_CREDENTIALS", DEFAULT_CREDENTIALS)
    )
    state_path = Path(
        os.environ.get("CLAUDE_TELEGRAM_NOTIFY_STATE") or _data_dir() / "events.sqlite3"
    )
    credentials = load_credentials(credentials_path)
    digest = fingerprint(payload)
    if not claim_event(state_path, digest):
        return
    try:
        _post_message(payload, credentials)
    except Exception:
        release_event(state_path, digest)
        raise
    mark_sent(state_path, digest)


def block_with_notify(session_id: str, args: str) -> int:
    try:
        message = handle_notify_command(session_id, args)
    except Exception as exc:  # block anyway: /notify must never reach the model
        message = f"claude-telegram-notify: error {type(exc).__name__}, state unchanged"
    print(message, file=sys.stderr)
    return EXIT_BLOCK


def handle_expansion(payload: dict[str, object]) -> int:
    session_id = str(payload.get("session_id") or "")
    # From a plugin the name may be namespaced: "telegram-notify:notify".
    name = str(payload.get("command_name") or "").lstrip("/").rsplit(":", 1)[-1]
    if name != NOTIFY_COMMAND:
        record_turn_start(session_id)
        return 0
    return block_with_notify(session_id, str(payload.get("command_args") or ""))


def handle_submit(payload: dict[str, object]) -> int:
    session_id = str(payload.get("session_id") or "")
    match = RAW_NOTIFY_PATTERN.match(str(payload.get("prompt") or ""))
    if match:
        return block_with_notify(session_id, match.group(1) or "")
    record_turn_start(session_id)
    return 0


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return 0
        event = payload.get("hook_event_name")
        if event == "UserPromptExpansion":
            return handle_expansion(payload)
        if event == "UserPromptSubmit":
            return handle_submit(payload)
        if event not in ("Stop", "StopFailure", "Notification"):
            return 0
        if event == "Stop" and work_in_progress(payload):
            return 0
        if (
            event == "Notification"
            and payload.get("notification_type") not in BLOCKING_NOTIFICATION_TYPES
        ):
            return 0
        if not switch_allows(payload):
            return 0
        deliver(payload)
    except Exception as exc:  # fail-open: the notifier must never block Claude
        print(f"claude-telegram-notify: {type(exc).__name__}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
