"""Test CLI end-to-end del notifier Claude Code → Telegram.

Unica dipendenza simulata: un server HTTP locale al posto di api.telegram.org.
Lo script viene eseguito come subprocess reale con payload JSON su stdin, quindi
vengono esercitati davvero CLI, filesystem, SQLite ed encoding form HTTP.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "telegram_notify.py"


class _RecordingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (nome imposto da BaseHTTPRequestHandler)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        with self.server.lock:
            self.server.requests.append(
                {"path": self.path, "form": {k: v[0] for k, v in parse_qs(body).items()}}
            )
            status = self.server.statuses.pop(0) if self.server.statuses else 200
        payload = b'{"ok": true}' if status == 200 else b'{"ok": false}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


class NotifierTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        self.server.requests = []
        self.server.statuses = []
        self.server.lock = threading.Lock()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        self.tmp = Path(tempfile.mkdtemp(prefix="claude-notify-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        self.credentials = self.tmp / "credentials.env"
        self.credentials.write_text(
            "TELEGRAM_BOT_TOKEN=TEST_TOKEN\nTELEGRAM_CHAT_ID=424242\n", encoding="utf-8"
        )
        self.credentials.chmod(0o600)
        self.state = self.tmp / "state" / "events.sqlite3"
        self.transcript = self.tmp / "transcript.jsonl"
        self.transcript.write_text('{"fixture": "innocua"}\n', encoding="utf-8")
        self.sessions = self.tmp / "state" / "sessions"
        self.default_file = self.tmp / "config" / "default"
        # Default "on" senza soglia: i test storici verificano l'invio incondizionato.
        self.set_default("on")

    def set_default(self, value: str | None) -> None:
        if value is None:
            self.default_file.unlink(missing_ok=True)
            return
        self.default_file.parent.mkdir(parents=True, exist_ok=True)
        self.default_file.write_text(value + "\n", encoding="utf-8")

    def run_notifier(
        self,
        payload: object,
        env_extra: dict[str, str] | None = None,
        env_drop: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("CLAUDE_PLUGIN_DATA", None)
        env.update(
            {
                "CLAUDE_TELEGRAM_NOTIFY_CREDENTIALS": str(self.credentials),
                "CLAUDE_TELEGRAM_NOTIFY_STATE": str(self.state),
                "CLAUDE_TELEGRAM_NOTIFY_API_BASE": f"http://127.0.0.1:{self.server.server_port}",
                "CLAUDE_TELEGRAM_NOTIFY_SESSIONS": str(self.sessions),
                "CLAUDE_TELEGRAM_NOTIFY_DEFAULT": str(self.default_file),
            }
        )
        env.update(env_extra or {})
        for key in env_drop:
            env.pop(key, None)
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def stop_payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "session_id": "session-123456789",
            "transcript_path": str(self.transcript),
            "cwd": "/tmp/my-project",
            "permission_mode": "default",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": "Ho completato il lavoro.",
            "user_prompt": "segreto che non deve uscire",
        }
        payload.update(overrides)
        return payload

    def state_rows(self) -> list[tuple[str, str]]:
        with sqlite3.connect(self.state) as connection:
            return connection.execute(
                "SELECT digest, status FROM delivered_event ORDER BY digest"
            ).fetchall()

    def notification_payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "session_id": "session-123456789",
            "transcript_path": str(self.transcript),
            "cwd": "/tmp/my-project",
            "hook_event_name": "Notification",
            "message": "Claude needs your permission to use Bash",
            "title": "Claude Code",
            "notification_type": "permission_prompt",
        }
        payload.update(overrides)
        return payload


class TelegramNotifyTest(NotifierTestBase):
    def test_stop_invia_notifica_senza_dati_privati(self) -> None:
        result = self.run_notifier(self.stop_payload())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.requests), 1)
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/botTEST_TOKEN/sendMessage")
        text = request["form"]["text"]
        self.assertTrue(
            text.startswith("✅ Claude · my-project · session-"), text
        )
        self.assertIn("Ho completato il lavoro.", text)
        self.assertNotIn("segreto che non deve uscire", text)

    def test_domanda_o_richiesta_esplicita_usa_punto_interrogativo(self) -> None:
        self.run_notifier(
            self.stop_payload(last_assistant_message="Preferisci il piano A o il piano B?")
        )
        self.run_notifier(
            self.stop_payload(
                session_id="session-987654321",
                last_assistant_message="Fammi sapere quando posso procedere.",
            )
        )

        self.assertEqual(len(self.server.requests), 2)
        for request in self.server.requests:
            self.assertTrue(request["form"]["text"].startswith("❓ Claude · "))

    def test_stop_con_background_task_attivi_non_notifica(self) -> None:
        result = self.run_notifier(
            self.stop_payload(
                background_tasks=[
                    {"id": "bash_1", "type": "shell", "status": "running", "description": "test suite"}
                ]
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.requests), 0)

    def test_stop_con_loop_schedulati_non_notifica(self) -> None:
        result = self.run_notifier(
            self.stop_payload(
                session_crons=[
                    {"id": "cron_1", "schedule": "dynamic", "recurring": True, "prompt": "/loop"}
                ]
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.requests), 0)

    def test_stop_con_task_terminati_notifica_comunque(self) -> None:
        result = self.run_notifier(
            self.stop_payload(
                background_tasks=[
                    {"id": "agent_1", "type": "subagent", "status": "completed", "description": "ricerca"},
                    {"id": "agent_2", "type": "subagent", "status": "failed", "description": "review"},
                ],
                session_crons=[],
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.requests), 1)

    def test_stop_failure_notifica_anche_con_task_attivi(self) -> None:
        result = self.run_notifier(
            self.stop_payload(
                hook_event_name="StopFailure",
                error="server_error",
                background_tasks=[
                    {"id": "bash_1", "type": "shell", "status": "running", "description": "build"}
                ],
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self.assertTrue(self.server.requests[0]["form"]["text"].startswith("⚠️ Claude · "))

    def test_stop_failure_invia_warning_con_errore_senza_dettagli(self) -> None:
        result = self.run_notifier(
            self.stop_payload(
                hook_event_name="StopFailure",
                error="rate_limit",
                error_details="dettagli riservati da non inoltrare",
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.requests), 1)
        text = self.server.requests[0]["form"]["text"]
        self.assertTrue(text.startswith("⚠️ Claude · my-project · session-"), text)
        self.assertIn("Errore API: rate_limit", text)
        self.assertIn("Ho completato il lavoro.", text)
        self.assertNotIn("dettagli riservati", text)

    def test_notification_di_blocco_invia_pausa(self) -> None:
        result = self.run_notifier(self.notification_payload())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.requests), 1)
        text = self.server.requests[0]["form"]["text"]
        self.assertTrue(text.startswith("⏸️ Claude · my-project · session-"), text)
        self.assertIn("Claude needs your permission to use Bash", text)

    def test_notification_tipi_non_bloccanti_ignorati(self) -> None:
        for kind, message in (
            ("idle_prompt", "Claude is waiting for your input"),
            ("auth_success", "Login riuscito"),
            ("agent_completed", "Agent finito"),
            ("elicitation_response", "Risposta ricevuta"),
        ):
            result = self.run_notifier(
                self.notification_payload(notification_type=kind, message=message)
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(len(self.server.requests), 0)

    def test_notification_dedupe_ma_tipi_distinti_notificano(self) -> None:
        self.run_notifier(self.notification_payload())
        self.run_notifier(self.notification_payload())
        self.run_notifier(
            self.notification_payload(
                notification_type="elicitation_dialog",
                message="Claude Code needs your input",
            )
        )

        self.assertEqual(len(self.server.requests), 2)
        rows = self.state_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row[1] for row in rows}, {"sent"})

    def test_evento_non_gestito_non_invia_nulla(self) -> None:
        result = self.run_notifier(self.stop_payload(hook_event_name="SubagentStop"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.requests), 0)

    def test_json_malformato_esce_zero_senza_post(self) -> None:
        result = self.run_notifier("questo non è JSON {{")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.server.requests), 0)
        self.assertNotIn("questo non è JSON", result.stderr)

    def test_messaggio_lungo_troncato_con_marker(self) -> None:
        result = self.run_notifier(self.stop_payload(last_assistant_message="A" * 5000))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.requests), 1)
        text = self.server.requests[0]["form"]["text"]
        self.assertLessEqual(len(text), 3900)
        self.assertTrue(text.endswith("[messaggio troncato]"), text[-60:])

    def test_credenziali_permessi_larghi_bloccano_invio(self) -> None:
        self.credentials.chmod(0o644)

        result = self.run_notifier(self.stop_payload())

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.server.requests), 0)
        self.assertNotIn("TEST_TOKEN", result.stderr)

    def test_credenziali_symlink_bloccano_invio(self) -> None:
        link = self.tmp / "credentials-link.env"
        link.symlink_to(self.credentials)
        env_override = {"CLAUDE_TELEGRAM_NOTIFY_CREDENTIALS": str(link)}
        env = os.environ.copy()
        env.update(
            {
                "CLAUDE_TELEGRAM_NOTIFY_STATE": str(self.state),
                "CLAUDE_TELEGRAM_NOTIFY_API_BASE": f"http://127.0.0.1:{self.server.server_port}",
                **env_override,
            }
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(self.stop_payload()),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.server.requests), 0)

    def test_errore_http_rilascia_claim_e_permette_retry(self) -> None:
        self.server.statuses = [500]

        first = self.run_notifier(self.stop_payload())
        self.assertEqual(first.returncode, 0)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.state_rows(), [])

        second = self.run_notifier(self.stop_payload())
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.server.requests), 2)
        rows = self.state_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "sent")

    def test_doppia_invocazione_identica_invia_una_sola_volta(self) -> None:
        self.run_notifier(self.stop_payload())
        self.run_notifier(self.stop_payload())

        self.assertEqual(len(self.server.requests), 1)
        rows = self.state_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "sent")

    def test_stesso_testo_dopo_append_transcript_invia_di_nuovo(self) -> None:
        self.run_notifier(self.stop_payload())
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write('{"fixture": "nuovo turno"}\n')
        self.run_notifier(self.stop_payload())

        self.assertEqual(len(self.server.requests), 2)
        rows = self.state_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row[1] for row in rows}, {"sent"})

    def test_processi_concorrenti_inviano_una_sola_volta(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "CLAUDE_TELEGRAM_NOTIFY_CREDENTIALS": str(self.credentials),
                "CLAUDE_TELEGRAM_NOTIFY_STATE": str(self.state),
                "CLAUDE_TELEGRAM_NOTIFY_API_BASE": f"http://127.0.0.1:{self.server.server_port}",
                "CLAUDE_TELEGRAM_NOTIFY_SESSIONS": str(self.sessions),
                "CLAUDE_TELEGRAM_NOTIFY_DEFAULT": str(self.default_file),
            }
        )
        stdin = json.dumps(self.stop_payload())
        processes = [
            subprocess.Popen(
                [sys.executable, str(SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(2)
        ]
        for process in processes:
            process.communicate(stdin, timeout=30)
            self.assertEqual(process.returncode, 0)

        self.assertEqual(len(self.server.requests), 1)
        rows = self.state_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "sent")

    def test_state_non_contiene_testi_o_path_in_chiaro(self) -> None:
        self.run_notifier(self.stop_payload())

        self.assertEqual(len(self.server.requests), 1)
        raw = self.state.read_bytes()
        self.assertNotIn(b"Ho completato il lavoro.", raw)
        self.assertNotIn(b"segreto che non deve uscire", raw)
        self.assertNotIn(str(self.transcript).encode("utf-8"), raw)
        self.assertNotIn(b"session-123456789", raw)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.parent.stat().st_mode), 0o700)
        for digest, status in self.state_rows():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertIn(status, ("sending", "sent"))


class NotifySwitchTest(NotifierTestBase):
    """Switch per sessione (/notify) e soglia minima di durata del turno."""

    SESSION = "session-123456789"

    def setUp(self) -> None:
        super().setUp()
        # Default di produzione: nessun file default → notifiche OFF.
        self.set_default(None)

    def notify_cmd(
        self, args: str, session_id: str = SESSION
    ) -> subprocess.CompletedProcess[str]:
        return self.run_notifier(
            {
                "session_id": session_id,
                "transcript_path": str(self.transcript),
                "cwd": "/tmp/my-project",
                "hook_event_name": "UserPromptExpansion",
                "expansion_type": "slash_command",
                "command_name": "notify",
                "command_args": args,
                "command_source": "user",
                "prompt": f"/notify {args}".strip(),
            }
        )

    def prompt_submit(self, session_id: str = SESSION) -> subprocess.CompletedProcess[str]:
        return self.run_notifier(
            {
                "session_id": session_id,
                "transcript_path": str(self.transcript),
                "cwd": "/tmp/my-project",
                "hook_event_name": "UserPromptSubmit",
                "prompt": "prompt privato",
            }
        )

    def session_files(self) -> list[Path]:
        return sorted(self.sessions.glob("*.json")) if self.sessions.exists() else []

    def backdate_turn(self, seconds: float) -> None:
        files = self.session_files()
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        data["turn_started_at"] -= seconds
        files[0].write_text(json.dumps(data), encoding="utf-8")

    def test_default_off_senza_file_default_non_invia(self) -> None:
        for payload in (
            self.stop_payload(),
            self.stop_payload(hook_event_name="StopFailure", error="rate_limit"),
            self.notification_payload(),
        ):
            result = self.run_notifier(payload)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.requests), 0)

    def test_notify_on_blocca_espansione_e_attiva_la_sessione(self) -> None:
        result = self.notify_cmd("on")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("ON", result.stderr)
        self.assertIn("no threshold", result.stderr)

        self.run_notifier(self.stop_payload())
        self.assertEqual(len(self.server.requests), 1)

    def test_notify_off_vince_sul_default_on(self) -> None:
        self.set_default("on")
        result = self.notify_cmd("off")

        self.assertEqual(result.returncode, 2)
        self.assertIn("OFF", result.stderr)
        for payload in (
            self.stop_payload(),
            self.stop_payload(hook_event_name="StopFailure", error="rate_limit"),
            self.notification_payload(),
        ):
            self.run_notifier(payload)
        self.assertEqual(len(self.server.requests), 0)

    def test_switch_isolato_per_sessione(self) -> None:
        self.notify_cmd("on")
        self.run_notifier(self.stop_payload(session_id="altra-sessione-000"))
        self.assertEqual(len(self.server.requests), 0)

    def test_soglia_turno_breve_non_invia_turno_lungo_invia(self) -> None:
        self.notify_cmd("on min 30")
        self.prompt_submit()
        self.run_notifier(self.stop_payload())
        self.assertEqual(len(self.server.requests), 0)

        self.backdate_turn(31)
        self.run_notifier(self.stop_payload())
        self.assertEqual(len(self.server.requests), 1)

    def test_soglia_applicata_anche_a_notification_e_stop_failure(self) -> None:
        self.notify_cmd("on min 30")
        self.prompt_submit()
        self.run_notifier(self.notification_payload())
        self.run_notifier(self.stop_payload(hook_event_name="StopFailure", error="x"))
        self.assertEqual(len(self.server.requests), 0)

        self.backdate_turn(45)
        self.run_notifier(self.notification_payload())
        self.assertEqual(len(self.server.requests), 1)
        self.assertTrue(self.server.requests[0]["form"]["text"].startswith("⏸️"))

    def test_nuovo_prompt_azzera_il_cronometro(self) -> None:
        self.notify_cmd("on min 30")
        self.prompt_submit()
        self.backdate_turn(120)
        self.prompt_submit()
        self.run_notifier(self.stop_payload())
        self.assertEqual(len(self.server.requests), 0)

    def test_altro_slash_command_avvia_il_turno_senza_output(self) -> None:
        self.notify_cmd("on min 30")
        self.prompt_submit()
        self.backdate_turn(120)
        result = self.run_notifier(
            {
                "session_id": self.SESSION,
                "hook_event_name": "UserPromptExpansion",
                "expansion_type": "slash_command",
                "command_name": "code-review",
                "command_args": "",
                "prompt": "/code-review",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.run_notifier(self.stop_payload())
        self.assertEqual(len(self.server.requests), 0)

    def test_soglia_senza_inizio_turno_registrato_notifica(self) -> None:
        self.notify_cmd("on min 30")
        self.run_notifier(self.stop_payload())
        self.assertEqual(len(self.server.requests), 1)

    def test_formati_soglia(self) -> None:
        for args, expected in (
            ("on min 30", "30s"),
            ("on min 45s", "45s"),
            ("on min 5m", "5m"),
            ("on 90", "1m30s"),
            ("on min=2m", "2m"),
            ("min 1h", "1h"),
            ("ON MIN 10S", "10s"),
        ):
            result = self.notify_cmd(args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("ON", result.stderr, args)
            self.assertIn(f"threshold {expected}", result.stderr, args)

    def test_on_senza_soglia_azzera_la_soglia_precedente(self) -> None:
        self.notify_cmd("on min 30")
        self.notify_cmd("on")
        self.prompt_submit()
        self.run_notifier(self.stop_payload())
        self.assertEqual(len(self.server.requests), 1)

    def test_stato_senza_argomenti_non_modifica(self) -> None:
        self.notify_cmd("on min 30")
        for args in ("", "status", "stato"):
            result = self.notify_cmd(args)
            self.assertEqual(result.returncode, 2)
            self.assertIn("ON", result.stderr)
            self.assertIn("threshold 30s", result.stderr)

    def test_stato_iniziale_mostra_default(self) -> None:
        result = self.notify_cmd("")
        self.assertEqual(result.returncode, 2)
        self.assertIn("OFF", result.stderr)
        self.assertIn("default", result.stderr)

    def test_argomenti_non_validi_mostrano_uso_e_non_modificano(self) -> None:
        self.notify_cmd("on min 30")
        for args in ("on min abc", "boh", "on min -5", "off min 30"):
            result = self.notify_cmd(args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("Usage:", result.stderr, args)
        status = self.notify_cmd("")
        self.assertIn("threshold 30s", status.stderr)

    def test_file_default_con_soglia(self) -> None:
        self.set_default("on min 60")
        self.prompt_submit()
        self.run_notifier(self.stop_payload())
        self.assertEqual(len(self.server.requests), 0)
        self.backdate_turn(61)
        self.run_notifier(self.stop_payload())
        self.assertEqual(len(self.server.requests), 1)

    def test_file_default_illeggibile_vale_off(self) -> None:
        self.set_default("sempre acceso")
        self.run_notifier(self.stop_payload())
        self.assertEqual(len(self.server.requests), 0)

    def test_stato_sessioni_privato_e_senza_id_in_chiaro(self) -> None:
        self.notify_cmd("on min 30")
        self.prompt_submit()
        files = self.session_files()
        self.assertEqual(len(files), 1)
        self.assertNotIn("session-123456789", files[0].name)
        content = files[0].read_text(encoding="utf-8")
        self.assertNotIn("session-123456789", content)
        self.assertNotIn("prompt privato", content)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.sessions.stat().st_mode), 0o700)

    def test_file_sessione_vecchi_vengono_eliminati(self) -> None:
        self.sessions.mkdir(parents=True)
        old = self.sessions / ("0" * 32 + ".json")
        old.write_text("{}", encoding="utf-8")
        eight_days_ago = old.stat().st_mtime - 8 * 86400
        os.utime(old, (eight_days_ago, eight_days_ago))

        self.prompt_submit()
        self.assertFalse(old.exists())
        self.assertEqual(len(self.session_files()), 1)

    def test_prompt_submit_esce_zero_senza_output(self) -> None:
        result = self.prompt_submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_file_sessione_corrotto_ripiega_sul_default(self) -> None:
        self.set_default("on")
        self.notify_cmd("off")
        self.session_files()[0].write_text("{non json", encoding="utf-8")
        self.run_notifier(self.stop_payload())
        self.assertEqual(len(self.server.requests), 1)


    def test_comando_con_namespace_plugin_intercettato(self) -> None:
        result = self.run_notifier(
            {
                "session_id": self.SESSION,
                "hook_event_name": "UserPromptExpansion",
                "command_name": "telegram-notify:notify",
                "command_args": "on",
                "command_source": "plugin",
            }
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("ON", result.stderr)
        self.run_notifier(self.stop_payload())
        self.assertEqual(len(self.server.requests), 1)

    def test_comando_con_nome_simile_non_intercettato(self) -> None:
        for name in ("notify-me", "other:notifyx", "notifications"):
            result = self.run_notifier(
                {
                    "session_id": self.SESSION,
                    "hook_event_name": "UserPromptExpansion",
                    "command_name": name,
                    "command_args": "on",
                }
            )
            self.assertEqual(result.returncode, 0, name)

    def test_stato_in_claude_plugin_data_senza_override(self) -> None:
        data = self.tmp / "plugin-data"
        env_extra = {"CLAUDE_PLUGIN_DATA": str(data)}
        drop = ("CLAUDE_TELEGRAM_NOTIFY_STATE", "CLAUDE_TELEGRAM_NOTIFY_SESSIONS")
        self.run_notifier(
            {
                "session_id": self.SESSION,
                "hook_event_name": "UserPromptExpansion",
                "command_name": "notify",
                "command_args": "on",
            },
            env_extra=env_extra,
            env_drop=drop,
        )
        self.run_notifier(self.stop_payload(), env_extra=env_extra, env_drop=drop)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(list((data / "sessions").glob("*.json"))), 1)
        self.assertTrue((data / "events.sqlite3").is_file())
        self.assertFalse(self.state.exists())


    def submit_raw(self, prompt: str) -> subprocess.CompletedProcess[str]:
        return self.run_notifier(
            {
                "session_id": self.SESSION,
                "hook_event_name": "UserPromptSubmit",
                "prompt": prompt,
            }
        )

    def test_notify_come_prompt_testuale_intercettato(self) -> None:
        for prompt in ("/notify on min 30", "  /notify on min 30\n", "/telegram-notify:notify on min 30"):
            result = self.submit_raw(prompt)
            self.assertEqual(result.returncode, 2, prompt)
            self.assertIn("threshold 30s", result.stderr, prompt)
        status = self.submit_raw("/notify")
        self.assertEqual(status.returncode, 2)
        self.assertIn("ON for this session", status.stderr)

    def test_prompt_che_menzionano_notify_non_intercettati(self) -> None:
        for prompt in ("/notifyx on", "come funziona /notify on?", "/notify-me", "notify on"):
            result = self.submit_raw(prompt)
            self.assertEqual(result.returncode, 0, prompt)
            self.assertEqual(result.stderr, "", prompt)


if __name__ == "__main__":
    unittest.main()
