import hashlib
import hmac
import json
import os
import socket
import stat
import subprocess
import tempfile
import threading
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


def read_frame(conn: socket.socket) -> dict:
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            raise AssertionError("peer closed before a full frame arrived")
        data += chunk
    return json.loads(data.split(b"\n", 1)[0])


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

    def test_connect_probes_then_sends_handshake(self):
        with tempfile.TemporaryDirectory() as tmp:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            path = os.path.join(tmp, "rpc.sock")
            listener.bind(path)
            listener.listen(1)
            host = HostClient(socket_path=path)
            errors: list[Exception] = []

            def connect() -> None:
                try:
                    host.connect()
                except Exception as exc:  # noqa: BLE001 - surfaced below
                    errors.append(exc)

            thread = threading.Thread(target=connect)
            thread.start()
            conn, _ = listener.accept()
            try:
                probe = read_frame(conn)
                self.assertEqual(probe["type"], "get_protocol_info")
                self.assertEqual(len(probe["auth"]["client_nonce"]), 64)
                conn.sendall((json.dumps({
                    "id": probe["id"], "type": "response",
                    "command": "get_protocol_info", "success": True,
                    "data": {"protocolVersion": 1,
                             "capabilities": ["multi_session"]},
                }) + "\n").encode())
                handshake = read_frame(conn)
                self.assertEqual(handshake["type"], "set_client_info")
            finally:
                conn.close()
            thread.join(timeout=5)
            try:
                self.assertFalse(thread.is_alive(), "connect stayed blocked")
                self.assertEqual(errors, [])
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

    def test_agent_end_alone_is_not_done(self):
        self.send({"type": "turn_end", "message": {"content": [
            {"type": "text", "text": "partial"}]}},
            {"type": "agent_end"})
        self.assertEqual(self.host.collect(timeout_s=1),
                         ("running", None, "partial"))

    def test_agent_settled_after_agent_end_is_done(self):
        self.send({"type": "turn_end", "message": {"content": [
            {"type": "text", "text": "done text"}]}},
            {"type": "agent_end"})
        self.assertEqual(self.host.collect(timeout_s=1),
                         ("running", None, "done text"))
        self.send({"type": "agent_settled"})
        self.assertEqual(self.host.collect(timeout_s=1), ("done", None, ""))

    def test_failed_response_raises_error_code(self):
        self.send(response("get_state-1", success=False, error="no such session",
                           error_code="session_not_found"))
        with self.assertRaises(HostError) as ctx:
            self.host.get_state("missing")
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(ctx.exception.detail, "no such session")


class TestEndpointAuth(unittest.TestCase):
    LAUNCH = "67a4bfb1-2992-4291-82f1-23b7b1ea3de6"

    def make_record(self, root: str, *, mode: int = 0o600,
                    key_hex: str = "ab" * 32) -> tuple[str, dict]:
        agent = os.path.join(root, "agent")
        keys = os.path.join(agent, "rpc", "keys")
        os.makedirs(keys)
        os.chmod(agent, 0o755)
        os.chmod(os.path.join(agent, "rpc"), 0o755)
        os.chmod(keys, 0o700)
        record = {"version": 1, "launch_id": self.LAUNCH, "key": key_hex,
                  "pid": os.getpid(), "process_start": None,
                  "endpoint": "/tmp/rpc.sock",
                  "created_at": "2026-01-01T00:00:00Z"}
        path = os.path.join(keys, self.LAUNCH)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh)
        os.chmod(path, mode)
        return agent, record

    def test_read_endpoint_record_rejects_wide_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = self.make_record(tmp, mode=0o644)
            with self.assertRaises(HostError) as ctx:
                host_client.read_endpoint_record(agent, self.LAUNCH)
            self.assertEqual(ctx.exception.code, "unsafe_key_record")

    def test_prove_endpoint_key_sends_the_client_proof(self):
        client_nonce = "11" * 32
        server_nonce = "22" * 32
        with tempfile.TemporaryDirectory() as tmp:
            agent, record = self.make_record(tmp)
            key = bytes.fromhex(record["key"])
            server_proof = hmac.new(
                key, "\n".join(["senpi-rpc-server", self.LAUNCH,
                                client_nonce, server_nonce]).encode(),
                hashlib.sha256).hexdigest()
            expected = hmac.new(
                key, "\n".join(["senpi-rpc-client", self.LAUNCH,
                                server_nonce, client_nonce]).encode(),
                hashlib.sha256).hexdigest()
            host = HostClient()
            sent: dict = {}

            def request(payload, timeout=15, keep_extra=True):
                sent.update(payload)
                return {"success": True, "data": {}}, []

            host.request = request
            host._prove_endpoint_key(
                {"agentDir": agent},
                {"launch_id": self.LAUNCH, "server_nonce": server_nonce,
                 "server_proof": server_proof},
                client_nonce)
            self.assertEqual(sent["type"], "authenticate")
            self.assertEqual(sent["client_proof"], expected)

    def test_prove_endpoint_key_rejects_a_wrong_server_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = self.make_record(tmp)
            host = HostClient()
            host.request = lambda *a, **k: self.fail("sent a command before proving the host")
            with self.assertRaises(HostError) as ctx:
                host._prove_endpoint_key(
                    {"agentDir": agent},
                    {"launch_id": self.LAUNCH, "server_nonce": "22" * 32,
                     "server_proof": "33" * 32},
                    "11" * 32)
            self.assertEqual(ctx.exception.code, "host_identity_unproven")

    def test_handshake_without_a_challenge_keeps_the_legacy_frame(self):
        host = HostClient()
        sent: list[dict] = []
        host.request = lambda payload, timeout=15, keep_extra=True: (
            {"success": True, "data": {"protocolVersion": 1,
                                       "capabilities": ["multi_session"]}}, [])
        host._send = sent.append
        host._handshake()
        self.assertEqual(sent[-1]["type"], "set_client_info")
        self.assertEqual(host.protocol_info()["protocolVersion"], 1)


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

    @mock.patch("omo.host_client._start_host_directly")
    @mock.patch("omo.host_client.subprocess.run")
    @mock.patch("omo.host_client.shutil.which", return_value="/usr/bin/omo")
    def test_ensure_host_starts_directly_before_the_supported_path(
            self, _which, run, direct):
        down = subprocess.CompletedProcess(args=[], returncode=3,
                                           stdout='{"reachable": false}', stderr="")
        run.side_effect = [down]
        direct.return_value = {"reachable": True, "pid": 9}
        self.assertEqual(host_client.ensure_host().get("pid"), 9)
        direct.assert_called_once()
        self.assertEqual(run.call_count, 1, "ensure must not run before the direct start")

    @mock.patch("omo.host_client._start_host_directly",
                side_effect=HostError("host_unavailable", "direct down"))
    @mock.patch("omo.host_client.subprocess.run")
    @mock.patch("omo.host_client.shutil.which", return_value="/usr/bin/omo")
    def test_ensure_host_falls_back_to_the_supported_path(self, _which, run, _direct):
        down = subprocess.CompletedProcess(args=[], returncode=3,
                                           stdout='{"reachable": false}', stderr="")
        up = subprocess.CompletedProcess(args=[], returncode=0,
                                         stdout='{"reachable": true, "pid": 9}', stderr="")
        run.side_effect = [down, up]
        self.assertEqual(host_client.ensure_host().get("pid"), 9)
        self.assertEqual(run.call_args_list[1].args[0][1:3], ["host", "ensure"])

    @mock.patch("omo.host_client._start_host_directly",
                side_effect=HostError("host_unavailable", "direct down"))
    @mock.patch("omo.host_client.subprocess.run")
    @mock.patch("omo.host_client.shutil.which", return_value="/usr/bin/omo")
    def test_ensure_host_retries_when_cold_start_misses_its_window(
            self, _which, run, _direct):
        down = subprocess.CompletedProcess(args=[], returncode=3,
                                           stdout='{"reachable": false}', stderr="")
        up = subprocess.CompletedProcess(args=[], returncode=0,
                                         stdout='{"reachable": true, "pid": 9}', stderr="")
        # status, failed ensure, probe still down, second ensure succeeds
        run.side_effect = [down, subprocess.TimeoutExpired(cmd="omo", timeout=30),
                           down, up]
        self.assertEqual(host_client.ensure_host().get("pid"), 9)
        self.assertEqual(run.call_args_list[-1].args[0][1:3], ["host", "ensure"])

    @mock.patch("omo.host_client.time.sleep")
    @mock.patch("omo.host_client.host_status")
    @mock.patch("omo.host_client._direct_host_log")
    @mock.patch("omo.host_client.subprocess.Popen")
    @mock.patch("omo.host_client.shutil.which", return_value="/usr/bin/omo")
    def test_direct_launch_runs_the_supervisor_child_command(
            self, _which, popen, log, status, _sleep):
        with tempfile.TemporaryDirectory() as tmp:
            log.return_value = os.path.join(tmp, "host.log")
            status.side_effect = [{"reachable": False},
                                  {"reachable": True, "pid": 7}]
            result = host_client._start_host_directly("/tmp/rpc.sock", timeout=1)
        self.assertEqual(result["pid"], 7)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:], ["--mode", "rpc", "--multi-session",
                                    "--listen", "unix:///tmp/rpc.sock"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    @mock.patch("omo.host_client._start_host_directly",
                side_effect=HostError("host_unavailable", "direct down"))
    @mock.patch("omo.host_client.subprocess.run")
    @mock.patch("omo.host_client.shutil.which", return_value="/usr/bin/omo")
    def test_ensure_host_maps_timeout(self, _which, run, _direct):
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


class TestStopHost(unittest.TestCase):
    @mock.patch("omo.host_client._remove_pid_record")
    @mock.patch("omo.host_client.time.sleep")
    @mock.patch("omo.host_client.time.monotonic")
    @mock.patch("omo.host_client._pid_alive")
    @mock.patch("omo.host_client.os.kill")
    @mock.patch("omo.host_client._is_host_pid", return_value=True)
    @mock.patch("omo.host_client._find_direct_host_pid", return_value=123)
    def test_term_then_remove_record(self, _find, _is, kill, alive, monotonic,
                                     _sleep, remove):
        alive.side_effect = [False, False]
        monotonic.side_effect = [0.0, 0.5]
        out = host_client.stop_host("/tmp/rpc.sock", timeout=1)
        self.assertTrue(out["stopped"])
        self.assertEqual(out["method"], "term")
        kill.assert_called_once_with(123, host_client.signal.SIGTERM)
        remove.assert_called_once_with("/tmp/rpc.sock")

    @mock.patch("omo.host_client._remove_pid_record")
    @mock.patch("omo.host_client._find_direct_host_pid", return_value=None)
    def test_no_host_is_not_an_error(self, _find, remove):
        out = host_client.stop_host("/tmp/rpc.sock")
        self.assertFalse(out["stopped"])
        self.assertEqual(out["reason"], "not_running")
        remove.assert_called_once_with("/tmp/rpc.sock")

    @mock.patch("omo.host_client._remove_pid_record")
    @mock.patch("omo.host_client._is_host_pid", return_value=False)
    @mock.patch("omo.host_client._find_direct_host_pid", return_value=123)
    def test_pid_mismatch_is_never_killed(self, _find, _is, remove):
        out = host_client.stop_host("/tmp/rpc.sock")
        self.assertFalse(out["stopped"])
        self.assertEqual(out["reason"], "pid_mismatch")
        remove.assert_called_once_with("/tmp/rpc.sock")


class TestDaemonGc(unittest.TestCase):
    @mock.patch("omo.host_client.run_daemon_gc",
                return_value={"ok": True, "output": "reaped: 1"})
    def test_gc_runs_once_per_interval(self, gc):
        with tempfile.TemporaryDirectory() as tmp:
            stamp = os.path.join(tmp, "last-gc")
            first = host_client.maybe_run_daemon_gc(interval_s=60, stamp_path=stamp)
            second = host_client.maybe_run_daemon_gc(interval_s=60, stamp_path=stamp)
        self.assertEqual(first["output"], "reaped: 1")
        self.assertIsNone(second)
        gc.assert_called_once()


class TestHostEnv(unittest.TestCase):
    @mock.patch("omo.host_client._private_tmpdir", return_value="/tmp/private-tmp")
    def test_overrides_all_temp_vars(self, _tmp):
        with mock.patch.dict(os.environ, {
                "TMPDIR": "/bad", "TMP": "/bad", "TEMP": "/bad",
                "SENPI_RPC_HOST_WATCH_X": "1"}):
            env = host_client._host_env()
        self.assertEqual(env["TMPDIR"], "/tmp/private-tmp")
        self.assertEqual(env["TMP"], "/tmp/private-tmp")
        self.assertEqual(env["TEMP"], "/tmp/private-tmp")
        self.assertNotIn("SENPI_RPC_HOST_WATCH_X", env)

    def test_harden_private_ancestors_removes_write_bits(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o777)
            host_client._harden_private_ancestors([root])
            mode = stat.S_IMODE(os.stat(root).st_mode)
        self.assertEqual(mode & 0o022, 0)
        self.assertEqual(mode & 0o700, 0o700)

    def test_ancestors_safe_accepts_private_path(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            os.chmod(root, 0o700)
            self.assertTrue(host_client._ancestors_safe(root))
            os.chmod(root, 0o775)
            self.assertFalse(host_client._ancestors_safe(root))

    @mock.patch("omo.host_client._private_tmpdir", return_value="/tmp/private-tmp")
    @mock.patch("omo.host_client.subprocess.run")
    def test_host_cmd_passes_the_private_env(self, run, _tmp):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"reachable": true}', stderr="")
        host_client._host_cmd(["/usr/bin/omo", "host", "status", "--json"], 5)
        self.assertEqual(run.call_args.kwargs["env"]["TMPDIR"], "/tmp/private-tmp")


if __name__ == "__main__":
    unittest.main()
