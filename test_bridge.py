import json
import io
import ssl
import socket
import urllib.error
from unittest.mock import patch
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

from bridge import Bridge, chunks, supported_form, validate_form, TelegramWorker, Telegram, TelegramError


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.ide, self.messages = [], [], []
        self.now = 100
        self.core = Bridge(self.agent.append, self.ide.append,
                           lambda text, buttons=None: self.messages.append((text, buttons)),
                           42, 42, permission_timeout=10, clock=lambda: self.now)
        self.request("initialize", {"protocolVersion": 1}, 0)
        self.reply({"protocolVersion": 1})
        self.request("session/new", {"cwd": "D:/mod", "mcpServers": []})
        self.reply({"sessionId": "s1"})
        self.command("/use s1")
        self.agent.clear()
        self.ide.clear()
        self.messages.clear()

    def request(self, method, params, id=7):
        self.core.from_ide({"jsonrpc": "2.0", "id": id, "method": method, "params": params})

    def reply(self, result):
        self.core.from_agent({"jsonrpc": "2.0", "id": self.agent[-1]["id"], "result": result})

    def command(self, text, user=42, chat=42, kind="private"):
        return self.core.telegram_update({"message": {"text": text, "from": {"id": user},
                                          "chat": {"id": chat, "type": kind}}})

    def update(self, kind, text, sid="s1"):
        self.core.from_agent({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid, "update": {"sessionUpdate": kind,
                                         "content": {"type": "text", "text": text}}}})

    def permission(self, id=7):
        self.core.from_agent({"jsonrpc": "2.0", "id": id, "method": "session/request_permission",
                             "params": {"sessionId": "s1", "toolCall": {"title": "Run Gradle"},
                                        "options": [{"optionId": "yes", "name": "Allow once", "kind": "allow_once"},
                                                    {"optionId": "no", "name": "Reject", "kind": "reject_once"}]}})
        return next(iter(self.core.pending))

    def callback(self, key, choice="0", user=42):
        return self.core.telegram_update({"callback_query": {"id": "cb", "from": {"id": user},
            "message": {"chat": {"id": 42, "type": "private"}}, "data": f"perm:{key}:{choice}"}})

    def test_transparent_request_response_preserves_unknown_fields_and_ids(self):
        self.core.from_ide({"jsonrpc": "2.0", "id": "same", "method": "extension/x",
                           "params": {"a": 1}, "_meta": {"x": True}})
        forwarded = self.agent[-1]
        self.assertNotEqual(forwarded["id"], "same")
        self.assertEqual(forwarded["_meta"], {"x": True})
        self.reply({"answer": 5})
        self.assertEqual(self.ide[-1]["id"], "same")

    def test_telegram_prompt_stream_and_completion(self):
        self.command("Проверь сборку")
        request = self.agent[-1]
        self.assertEqual(request["method"], "session/prompt")
        self.assertEqual(request["params"]["prompt"][0]["text"], "Проверь сборку")
        self.assertEqual(self.ide[-1]["params"]["update"]["sessionUpdate"], "user_message_chunk")
        self.update("agent_message_chunk", "Готово 🐈")
        self.now += 4
        self.core.tick()
        self.assertIn("Готово 🐈", [text for text, _ in self.messages])
        self.core.from_agent({"id": request["id"], "result": {"stopReason": "end_turn"}})
        self.assertIsNone(self.core.sessions["s1"].active)
        self.assertFalse(any(m.get("id") == request["id"] for m in self.ide))

    def test_unknown_user_group_and_edited_message_cannot_send_prompt(self):
        for user, chat, kind in [(9, 42, "private"), (42, 9, "private"), (42, 42, "group")]:
            self.assertFalse(self.command("danger", user, chat, kind))
        self.core.telegram_update({"edited_message": {"text": "danger"}})
        self.assertEqual(self.agent, [])

    def test_busy_remote_and_ide_prompts_are_rejected(self):
        self.command("first")
        self.command("second")
        self.request("session/prompt", {"sessionId": "s1", "prompt": []}, 12)
        self.assertEqual(len(self.agent), 1)
        self.assertIn("error", self.ide[-1])
        self.assertEqual(self.ide[-1]["id"], 12)

    def test_busy_ide_blocks_remote_and_finishes_with_original_id(self):
        self.request("session/prompt", {"sessionId": "s1", "prompt": []}, 0)
        self.command("second")
        self.assertEqual(len(self.agent), 1)
        self.reply({"stopReason": "end_turn"})
        self.assertEqual(self.ide[-1]["id"], 0)
        self.assertIsNone(self.core.sessions["s1"].active)

    def test_permissions_require_explicit_owner_action_and_ignore_duplicates(self):
        self.command("task")
        key = self.permission()
        before = len(self.agent)
        self.callback(key, user=999)
        self.assertEqual(len(self.agent), before)
        self.callback(key)
        self.assertEqual(self.agent[-1], {"jsonrpc": "2.0", "id": 7,
            "result": {"outcome": {"outcome": "selected", "optionId": "yes"}}})
        self.callback(key)
        self.assertEqual(len(self.agent), before + 1)
        self.assertFalse(any(m.get("method") == "session/request_permission" for m in self.ide))

    def test_permission_expiry_and_cancel(self):
        self.command("task")
        self.permission()
        self.now += 11
        self.core.tick()
        self.assertEqual(self.agent[-1]["result"]["outcome"]["outcome"], "cancelled")
        self.permission(19)
        self.command("/cancel")
        self.assertEqual(self.agent[-2]["id"], 19)
        self.assertEqual(self.agent[-1]["method"], "session/cancel")
        self.assertIsNotNone(self.core.sessions["s1"].active)

    def test_forms_validate_choices_and_relay_explicit_answer(self):
        self.command("task")
        schema = {"type": "object", "properties": {"choice": {"type": "string", "enum": ["A", "B"]}},
                  "required": ["choice"]}
        self.core.from_agent({"id": 44, "method": "elicitation/create", "params": {
            "sessionId": "s1", "mode": "form", "message": "Which?", "requestedSchema": schema}})
        key = next(iter(self.core.pending))
        self.command(f'/answer {key} {{"choice":"C"}}')
        self.assertIn(key, self.core.pending)
        self.command(f'/answer {key} {{"choice":"A"}}')
        self.assertEqual(self.agent[-1]["result"], {"action": "accept", "content": {"choice": "A"}})

    def test_unknown_form_goes_to_ide_with_response_id_translated(self):
        self.core.from_agent({"id": "q", "method": "elicitation/create", "params": {
            "sessionId": "s1", "mode": "url", "url": "https://example.org"}})
        wire = self.ide[-1]["id"]
        self.assertNotEqual(wire, "q")
        self.core.from_ide({"id": wire, "result": {"action": "cancel"}})
        self.assertEqual(self.agent[-1]["id"], "q")

    def test_authentication_parent_request_id_translated(self):
        self.request("authenticate", {"methodId": "chatgpt"}, 84)
        self.core.from_agent({"id": 1, "method": "elicitation/create", "params": {
            "requestId": self.agent[-1]["id"], "mode": "url", "url": "https://example.org"}})
        self.assertEqual(self.ide[-1]["params"]["requestId"], 84)

    def test_bidirectional_request_cancel_translation(self):
        self.request("example", {}, 11)
        wire = self.agent[-1]["id"]
        self.core.from_ide({"method": "$/cancel_request", "params": {"requestId": 11}})
        self.assertEqual(self.agent[-1]["params"]["requestId"], wire)
        self.core.from_agent({"id": 7, "method": "fs/read_text_file", "params": {"path": "x"}})
        wire = self.ide[-1]["id"]
        self.core.from_agent({"method": "$/cancel_request", "params": {"requestId": 7}})
        self.assertEqual(self.ide[-1]["params"]["requestId"], wire)

    def test_cancelled_permission_cannot_be_approved_later(self):
        key = self.permission(7)
        self.core.from_agent({"method": "$/cancel_request", "params": {"requestId": 7}})
        count = len(self.agent)
        self.callback(key)
        self.assertEqual(len(self.agent), count)

    def test_fs_and_mcp_are_forwarded_without_interpretation(self):
        for method in ("fs/read_text_file", "mcp/connect", "mcp/message", "terminal/create"):
            self.core.from_agent({"jsonrpc": "2.0", "id": "repeat", "method": method,
                                 "params": {"sessionId": "s1", "opaque": {"data": "preserved"}}})
            message = self.ide[-1]
            self.assertEqual(message["params"]["opaque"], {"data": "preserved"})
            self.core.from_ide({"id": message["id"], "result": {"test": True}})
            self.assertEqual(self.agent[-1]["id"], "repeat")

    def test_unselected_chats_history_and_thoughts_not_sent_to_telegram(self):
        self.update("agent_message_chunk", "history")
        self.command("task")
        self.messages.clear()
        self.update("agent_thought_chunk", "internal")
        self.update("agent_message_chunk", "other chat", "s2")
        self.now += 5
        self.core.tick()
        self.assertEqual(self.messages, [])

    def test_close_unbinds_session(self):
        self.request("session/close", {"sessionId": "s1"})
        self.reply({})
        self.assertIsNone(self.core.selected)
        self.assertNotIn("s1", self.core.sessions)

    def test_disconnection_fails_pending_ide_request(self):
        self.request("example", {}, 9)
        self.core.disconnected()
        self.assertEqual(self.ide[-1]["id"], 9)
        self.assertIn("error", self.ide[-1])
        count = len(self.agent)
        self.command("new task")
        self.assertEqual(len(self.agent), count)

    def test_load_tracks_existing_session(self):
        self.request("session/load", {"sessionId": "restored", "cwd": "D:/existing", "mcpServers": []})
        self.reply({})
        self.assertEqual(self.core.sessions["restored"].cwd, "D:/existing")

    def test_request_id_collision_between_ide_and_bot(self):
        self.command("task")
        bot_id = self.agent[-1]["id"]
        self.request("extension", {}, bot_id)
        ide_id = self.agent[-1]["id"]
        self.assertNotEqual(ide_id, bot_id)
        self.core.from_agent({"id": ide_id, "result": {}})
        self.assertEqual(self.ide[-1]["id"], bot_id)
        self.assertIsNotNone(self.core.sessions["s1"].active)


class UtilityTests(unittest.TestCase):
    def test_unicode_chunks_fit_telegram_limits(self):
        text = "🐈Я" * 5000
        result = list(chunks(text))
        self.assertEqual("".join(result), text)
        self.assertTrue(all(len(x.encode("utf-16-le")) // 2 <= 3500 for x in result))

    def test_complex_and_secret_forms_use_ide(self):
        for prop in ({"type": "string", "pattern": ".*"},
                     {"type": "string", "_meta": {"codex": {"isSecret": True}}}):
            self.assertFalse(supported_form({"mode": "form", "requestedSchema": {
                "type": "object", "properties": {"x": prop}}}))

    def test_numeric_validation_rejects_bool_and_nan(self):
        schema = {"type": "object", "properties": {"x": {"type": "number"}}}
        self.assertFalse(validate_form(schema, {"x": True}))
        self.assertFalse(validate_form(schema, {"x": float("nan")}))
        self.assertTrue(validate_form(schema, {"x": 12}))

    def test_poll_deduplicates_and_skips_messages_before_restart(self):
        stop = threading.Event()
        received = []
        class Core:
            def telegram_update(self, update):
                received.append(update["update_id"])
        class Api:
            def call(self, method, **params):
                stop.set()
                return [{"update_id": 1, "message": {"date": 1}},
                        {"update_id": 2, "message": {"date": 100}},
                        {"update_id": 2, "message": {"date": 100}}]
        worker = TelegramWorker(Api(), Core(), 42, stop)
        worker.started = 100
        worker.poll_loop()
        self.assertEqual(received, [2])


class TelegramNetworkTests(unittest.TestCase):
    def test_windows_certificate_failure_uses_verified_curl(self):
        api = Telegram("000:test-secret")
        with patch("bridge.urllib.request.urlopen", side_effect=urllib.error.URLError(
                ssl.SSLCertVerificationError("untrusted"))), \
                patch("bridge.os.name", "nt"), patch.object(Path, "is_file", return_value=True), \
                patch.object(api, "call_curl", return_value={"id": 10}) as fallback:
            self.assertEqual(api.call("getMe"), {"id": 10})
            fallback.assert_called_once_with("getMe", {})
            self.assertTrue(api.use_curl)

    def test_timeout_reports_reason_without_sensitive_url(self):
        secret = "123:never-print-this"
        with patch("bridge.urllib.request.urlopen", side_effect=urllib.error.URLError(
                TimeoutError("https://api.telegram.org/bot" + secret))):
            with self.assertRaises(TelegramError) as error:
                Telegram(secret).call("getMe")
        self.assertNotIn(secret, str(error.exception))
        self.assertIn("время", str(error.exception))

    def test_dns_failure_reports_dns(self):
        with patch("bridge.urllib.request.urlopen", side_effect=urllib.error.URLError(socket.gaierror())):
            with self.assertRaises(TelegramError) as error:
                Telegram("0:fake").call("getMe")
        self.assertIn("DNS", str(error.exception))

    def test_http_401_does_not_retry_or_expose_description(self):
        failure = urllib.error.HTTPError("https://secret", 401, "invalid", {},
            io.BytesIO(b'{"ok":false,"error_code":401,"description":"sensitive"}'))
        api = Telegram("0:fake")
        with patch("bridge.urllib.request.urlopen", side_effect=failure), \
                patch.object(api, "call_curl") as fallback:
            with self.assertRaises(TelegramError) as error:
                api.call("getMe")
            fallback.assert_not_called()
        self.assertEqual(error.exception.code, 401)
        self.assertNotIn("sensitive", str(error.exception))

    def test_curl_keeps_token_out_of_arguments_and_preserves_json(self):
        api = Telegram("123:secret")
        result = subprocess.CompletedProcess([], 0, b'{"ok":true,"result":{"id":7}}\n200', b'')
        params = {"text": 'Привет "кот"\nD:\\x'}
        with patch("bridge.subprocess.run", return_value=result) as run:
            self.assertEqual(api.call_curl("getMe", params), {"id": 7})
        args = run.call_args.args[0]
        config = run.call_args.kwargs["input"].decode()
        self.assertNotIn("123:secret", " ".join(args))
        self.assertNotIn("--insecure", args)
        self.assertEqual(args[1], "--disable")
        lines = config.splitlines()
        self.assertEqual(len(lines), 3)
        # curl configuration string escaping here matches JSON string escapes.
        encoded_body = json.loads(lines[2].split(" = ", 1)[1])
        self.assertEqual(json.loads(encoded_body), params)

    def test_curl_tls_error_is_not_silenced(self):
        result = subprocess.CompletedProcess([], 60, b'', b'secret URL')
        with patch("bridge.subprocess.run", return_value=result):
            with self.assertRaises(TelegramError) as error:
                Telegram("0:fake").call_curl("getMe", {})
        self.assertIn("сертификату", str(error.exception))
        self.assertNotIn("secret", str(error.exception))

    def test_curl_preserves_rate_limit_backoff(self):
        result = subprocess.CompletedProcess([], 0,
            b'{"ok":false,"error_code":429,"parameters":{"retry_after":19}}\n429', b'')
        with patch("bridge.subprocess.run", return_value=result):
            with self.assertRaises(TelegramError) as error:
                Telegram("0:fake").call_curl("getMe", {})
        self.assertEqual(error.exception.code, 429)
        self.assertEqual(error.exception.retry_after, 19)


class ProcessTest(unittest.TestCase):
    def test_real_stdio_proxy_with_fake_agent_and_clean_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fake.py"
            fake.write_text('''import json, sys
for line in sys.stdin.buffer:
 m=json.loads(line)
 if "id" in m:
  print(json.dumps({"jsonrpc":"2.0", "id":m["id"], "result":{"echo":m.get("params")}}), flush=True)
''', encoding="utf-8")
            config = root / "config.json"
            config.write_text(json.dumps({"owner_id": 42, "chat_id": 42,
                "agent_command": [sys.executable, str(fake)]}), encoding="utf-8")
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("bridge.py")),
                "--config", str(config), "--stdio-only"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                request = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"text": "Привет 🐈"}}
                responses = queue.Queue()
                def receive():
                    responses.put(process.stdout.readline())
                threading.Thread(target=receive, daemon=True).start()
                process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
                process.stdin.flush()
                response = json.loads(responses.get(timeout=10))
                self.assertEqual(response["id"], 0)
                self.assertEqual(response["result"]["echo"], request["params"])
                output, stderr = process.communicate(timeout=15)
                self.assertEqual(output, b"")
                self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
