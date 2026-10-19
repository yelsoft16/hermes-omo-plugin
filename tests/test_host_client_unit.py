import json
import os
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from omo import host_client
from omo.host_client import (
    HostClient,
    HostError,
    OpenResult,
    check_protocol,
    default_socket_path,
    parse_open_response,
)


def response(rid: str, data: dict | None = None, success: bool = True,
             error: str | None = None, error_code: str | None = None) -> dict:
    frame = {"id": rid, "type": "response", "success": success}
    if data is not None:
        frame["data"] = data
    if error is not None:
        frame["error"] = error
    if error_code is not None:
        frame["errorCode"] = error_code
    return frame


def question(ui_request_id: str) -> dict:
    return {"type": "extension_ui_request", "method": "question",
            "uiRequestId": ui_request_id, "sessionId": "s-1"}


class TestProtocol(unittest.TestCase):
    def test_default_socket_path(self):
        self.assertTrue(default_socket_path().endswith("/.omo/agent/rpc/rpc.sock"))

    def test_check_protocol_accepts_v1(self):
        check_protocol({
            "protocolVersion": 1,
            "capabilities": ["multi_session", "retain_on_disconnect"],
        })

    def test_check_protocol_rejects_wrong_version(self):
        with self.assertRaises(HostError) as ctx:
            check_protocol({"protocolVersion": 2, "capabilities": []})
        self.assertEqual(ctx.exception.code, "protocol_mismatch")

    def test_check_protocol_rejects_missing_capability(self):
        with self.assertRaises(HostError) as ctx:
            check_protocol({"protocolVersion": 1, "capabilities": ["multi_session"]})
        self.assertEqual(ctx.exception.code, "protocol_mismatch")

    def test_parse_open_response_uses_state_session_file(self):
        opened = parse_open_response({
            "success": True,
            "data": {
                "sessionId": "rpc-3",
                "sessionPath": "/tmp/decoy.jsonl",
                "state": {"sessionFile": "/tmp/session.jsonl", "sessionId": "durable-1",
                          "pendingQuestions": []},
            },
        })
        self.assertIsInstance(opened, OpenResult)
        self.assertEqual(opened.session_path, "/tmp/session.jsonl")
        self.assertEqual(opened.durable_session_id, "durable-1")
        self.assertEqual(opened.session_id, "rpc-3")

    def test_parse_open_response_falls_back_to_given_path(self):
        opened = parse_open_response({"success": True, "data": {"sessionId": "rpc-4"}},
                                     fallback_path="/tmp/old.jsonl")
        self.assertEqual(opened.session_path, "/tmp/old.jsonl")


class TestHostClient(unittest.TestCase):
    """Exercises HostClient over socketpair endpoints; no OmO host needed."""

    def setUp(self):
        self.peer, self.sock = socket.socketpair()
        self._rx = b""
        self.host = HostClient()
        self.host.attach_socket(self.sock)
        self.handshake = self.read_frame()

    def tearDown(self):
        self.host.close()
        self.peer.close()

    def read_frame(self) -> dict:
        while b"\n" not in self._rx:
            chunk = self.peer.recv(65536)
            if not chunk:
                self.fail("peer closed before a full frame arrived")
            self._rx += chunk
        line, self._rx = self._rx.split(b"\n", 1)
        return json.loads(line)

    def send(self, *frames: dict) -> None:
        self.peer.sendall(b"".join(json.dumps(f).encode("utf-8") + b"\n"
                                   for f in frames))

    def test_attach_socket_sends_handshake(self):
        self.assertEqual(self.handshake["id"], "ci")
        self.assertEqual(self.handshake["type"], "set_client_info")
        self.assertEqual(self.handshake["capabilities"], ["question"])

    def test_connect_uses_unix_socket_and_sends_handshake(self):
        with tempfile.TemporaryDirectory() as tmp:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            path = os.path.join(tmp, "rpc.sock")
            listener.bind(path)
            listener.listen(1)
            host = HostClient(socket_path=path)
            try:
                host.connect()
                conn, _ = listener.accept()
                try:
                    data = b""
                    while b"\n" not in data:
                        data += conn.recv(65536)
                    frame = json.loads(data.split(b"\n", 1)[0])
                finally:
                    conn.close()
                self.assertEqual(frame["type"], "set_client_info")
            finally:
                host.close()
                listener.close()

    def test_open_session_payload_and_buffered_question(self):
        self.send(question("q-before"), response("open_session-1", {
            "sessionId": "rpc-7",
            "state": {"sessionFile": "/tmp/new.jsonl", "sessionId": "durable-7",
                      "pendingQuestions": []},
        }), question("q-after"))
        opened = self.host.open_session(cwd="/work", session_path="/tmp/old.jsonl",
                                        name="demo", permission="auto",
                                        provider="anthropic", model_id="m-1")
        sent = self.read_frame()
        self.assertEqual(sent["type"], "open_session")
        self.assertEqual(sent["cwd"], "/work")
        self.assertEqual(sent["sessionPath"], "/tmp/old.jsonl")
        self.assertNotIn("promptSurface", sent)
        self.assertEqual(sent["name"], "demo")
        self.assertEqual(sent["permissionPreset"], "auto")
        self.assertEqual(sent["provider"], "anthropic")
        self.assertEqual(sent["modelId"], "m-1")
        self.assertEqual(opened.session_id, "rpc-7")
        self.assertEqual(opened.session_path, "/tmp/new.jsonl")
        self.assertEqual(opened.durable_session_id, "durable-7")
        self.assertEqual([q["uiRequestId"] for q in opened.buffered], ["q-before"])
        status, frame, text = self.host.collect(timeout_s=1)
        self.assertEqual(status, "question")
        self.assertEqual(frame["uiRequestId"], "q-after")
        self.assertEqual(len(self.host._pending), 0)

    def test_prompt_keeps_question_that_arrived_before_response(self):
        self.send(question("q-1"), response("prompt-1", {"disposition": "queued"}))
        self.assertEqual(self.host.prompt("s-1", "hi"), "queued")
        sent = self.read_frame()
        self.assertEqual(sent["type"], "prompt")
        self.assertEqual(sent["sessionId"], "s-1")
        status, frame, text = self.host.collect(timeout_s=1)
        self.assertEqual(status, "question")
        self.assertEqual(frame["uiRequestId"], "q-1")
        self.assertEqual(text, "")

    def test_prompt_keeps_question_sent_after_response_in_one_chunk(self):
        self.send(response("prompt-1", {"disposition": "queued"}), question("q-2"))
        self.assertEqual(self.host.prompt("s-1", "hi"), "queued")
        self.read_frame()
        status, frame, text = self.host.collect(timeout_s=1)
        self.assertEqual(status, "question")
        self.assertEqual(frame["uiRequestId"], "q-2")
        self.assertEqual(text, "")

    def test_answer_required_fields_win_over_body(self):
        self.send(response("extension_ui_response-1", {"ok": True}))
        self.host.answer("s-1", "q-1", {"type": "evil", "sessionId": "evil",
                                        "uiRequestId": "evil", "answer": "yes"})
        sent = self.read_frame()
        self.assertEqual(sent["type"], "extension_ui_response")
        self.assertEqual(sent["sessionId"], "s-1")
        self.assertEqual(sent["uiRequestId"], "q-1")
        self.assertEqual(sent["answer"], "yes")

    def test_collect_running_question_and_turn_end_text(self):
        self.assertEqual(self.host.collect(timeout_s=1), ("running", None, ""))

        self.send(question("q-3"))
        status, frame, text = self.host.collect(timeout_s=1)
        self.assertEqual(status, "question")
        self.assertEqual(frame["uiRequestId"], "q-3")
        self.assertEqual(text, "")

        self.send({"type": "turn_end", "message": {"content": [
            {"type": "text", "text": "hel"}, {"type": "text", "text": "lo"}]}},
            {"type": "agent_settled"})
        self.assertEqual(self.host.collect(timeout_s=1), ("done", None, "hello"))

    def test_failed_response_raises_error_code(self):
        self.send(response("get_state-1", success=False, error="no such session",
                           error_code="session_not_found"))
        with self.assertRaises(HostError) as ctx:
            self.host.get_state("missing")
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(ctx.exception.detail, "no such session")


class TestHostCommandErrors(unittest.TestCase):
    @mock.patch("omo.host_client.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="omo", timeout=1))
    @mock.patch("omo.host_client.shutil.which", return_value="/usr/bin/omo")
    def test_host_status_maps_timeout(self, _which, _run):
        with self.assertRaises(HostError) as ctx:
            host_client.host_status(timeout=1)
        self.assertEqual(ctx.exception.code, "host_unavailable")

    @mock.patch("omo.host_client.subprocess.run")
    @mock.patch("omo.host_client.shutil.which", return_value="/usr/bin/omo")
    def test_host_status_maps_unparseable_output(self, _which, run):
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0,
                                                       stdout="not json", stderr="")
        with self.assertRaises(HostError) as ctx:
            host_client.host_status()
        self.assertEqual(ctx.exception.code, "host_unavailable")

    @mock.patch("omo.host_client.subprocess.run")
    @mock.patch("omo.host_client.shutil.which", return_value="/usr/bin/omo")
    def test_host_status_accepts_exit_code_3_when_host_is_down(self, _which, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=3,
            stdout='{"reachable": false, "socket": "/tmp/rpc.sock"}', stderr="")
        self.assertFalse(host_client.host_status().get("reachable"))

    @mock.patch("omo.host_client.subprocess.run")
    @mock.patch("omo.host_client.shutil.which", return_value="/usr/bin/omo")
    def test_host_status_rejects_unrelated_json_on_exit_code_3(self, _which, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=3, stdout='{"error": "something else"}', stderr="")
        with self.assertRaises(HostError) as ctx:
            host_client.host_status()
        self.assertEqual(ctx.exception.code, "host_unavailable")

    @mock.patch("omo.host_client.subprocess.run")
    @mock.patch("omo.host_client.shutil.which", return_value="/usr/bin/omo")
    def test_ensure_host_starts_when_status_reports_down(self, _which, run):
        down = subprocess.CompletedProcess(args=[], returncode=3,
                                           stdout='{"reachable": false}', stderr="")
        up = subprocess.CompletedProcess(args=[], returncode=0,
                                         stdout='{"reachable": true, "pid": 9}', stderr="")
        run.side_effect = [down, up]
        self.assertEqual(host_client.ensure_host().get("pid"), 9)
        self.assertEqual(run.call_args_list[1].args[0][1:3], ["host", "ensure"])

    @mock.patch("omo.host_client.subprocess.run")
    @mock.patch("omo.host_client.shutil.which", return_value="/usr/bin/omo")
    def test_ensure_host_retries_when_cold_start_misses_its_window(self, _which, run):
        down = subprocess.CompletedProcess(args=[], returncode=3,
                                           stdout='{"reachable": false}', stderr="")
        up = subprocess.CompletedProcess(args=[], returncode=0,
                                         stdout='{"reachable": true, "pid": 9}', stderr="")
        # status, failed ensure, probe still down, second ensure succeeds
        run.side_effect = [down, subprocess.TimeoutExpired(cmd="omo", timeout=30),
                           down, up]
        self.assertEqual(host_client.ensure_host().get("pid"), 9)
        self.assertEqual(run.call_args_list[-1].args[0][1:3], ["host", "ensure"])

    @mock.patch("omo.host_client.subprocess.run")
    @mock.patch("omo.host_client.shutil.which", return_value="/usr/bin/omo")
    def test_ensure_host_maps_timeout(self, _which, run):
        status = subprocess.CompletedProcess(args=[], returncode=0,
                                             stdout='{"reachable": false}', stderr="")
        down = subprocess.CompletedProcess(args=[], returncode=3,
                                           stdout='{"reachable": false}', stderr="")
        run.side_effect = [status,
                           subprocess.TimeoutExpired(cmd="omo", timeout=30), down,
                           subprocess.TimeoutExpired(cmd="omo", timeout=30), down]
        with self.assertRaises(HostError) as ctx:
            host_client.ensure_host()
        self.assertEqual(ctx.exception.code, "host_unavailable")


if __name__ == "__main__":
    unittest.main()
