import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo import tools
from omo.host_client import HostError, OpenResult, TransportGone
from omo.sessions import SessionStore


class TestToolHelpers(unittest.TestCase):
    def test_split_model(self):
        self.assertEqual(tools._split_model("omniroute/glm-5.3-flash"),
                         ("omniroute", "glm-5.3-flash"))
        self.assertEqual(tools._split_model("glm-5.3-flash"), (None, "glm-5.3-flash"))
        self.assertEqual(tools._split_model(None), (None, None))

    def test_validate_permission_accepts_known(self):
        for name in ("full-access", "workspace", "accept-edits", "auto", "read-only", "ask"):
            tools._validate_permission(name)

    def test_validate_permission_rejects_unknown(self):
        with self.assertRaises(Exception) as ctx:
            tools._validate_permission("root")
        self.assertEqual(getattr(ctx.exception, "code", ""), "invalid_permission")

    def test_resolve_cwd_requires_absolute(self):
        with self.assertRaises(Exception) as ctx:
            tools._resolve_cwd("relative/path")
        self.assertEqual(getattr(ctx.exception, "code", ""), "invalid_cwd")

    def test_resolve_cwd_rejects_missing(self):
        with self.assertRaises(Exception) as ctx:
            tools._resolve_cwd("/definitely/not/here")
        self.assertEqual(getattr(ctx.exception, "code", ""), "invalid_cwd")

    def test_resolve_cwd_accepts_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(tools._resolve_cwd(tmp), os.path.realpath(tmp))

    def test_resolve_cwd_expands_home(self):
        self.assertEqual(tools._resolve_cwd("~"),
                         os.path.realpath(os.path.expanduser("~")))

    def test_timeout_zero_is_clamped_to_one(self):
        self.assertEqual(tools._timeout({"timeout_s": 0}), 1)

    def test_allowed_roots_handles_filesystem_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(tools, "_cfg",
                              side_effect=lambda key, default:
                              ["/"] if key == "allowed_roots" else default):
                self.assertEqual(tools._resolve_cwd(tmp), os.path.realpath(tmp))

    def test_allowed_roots_rejects_outside(self):
        with tempfile.TemporaryDirectory() as allowed, \
                tempfile.TemporaryDirectory() as other:
            with patch.object(tools, "_cfg",
                              side_effect=lambda key, default:
                              [allowed] if key == "allowed_roots" else default):
                with self.assertRaises(Exception) as ctx:
                    tools._resolve_cwd(other)
            self.assertEqual(getattr(ctx.exception, "code", ""), "invalid_cwd")


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeHost:
    def __init__(self, *, open_result: OpenResult | None = None,
                 prompt_error: Exception | None = None,
                 state_error: Exception | None = None,
                 abort_error: Exception | None = None,
                 collect_results: list | None = None,
                 clock: FakeClock | None = None,
                 collect_advance: list[float] | None = None) -> None:
        self.open_result = open_result or OpenResult(
            session_id="rpc-1", session_path="/tmp/omo-session.json",
            durable_session_id="durable-1", state={}, buffered=[])
        self.prompt_error = prompt_error
        self.state_error = state_error
        self.abort_error = abort_error
        self.collect_results = list(collect_results or [])
        self.clock = clock
        self.collect_advance = list(collect_advance or [])
        self.open_calls = 0
        self.open_kwargs: dict = {}
        self.prompt_calls: list[tuple[str, str]] = []
        self.closed_sessions: list[str] = []
        self.aborted: list[str] = []
        self.collect_timeouts: list[float] = []
        self.closed = 0

    def __enter__(self) -> "FakeHost":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.closed += 1

    def protocol_info(self) -> dict:
        return {"protocolVersion": 1,
                "capabilities": ["multi_session", "retain_on_disconnect"]}

    def open_session(self, **kwargs) -> OpenResult:
        self.open_calls += 1
        self.open_kwargs = kwargs
        return self.open_result

    def prompt(self, session_id: str, message: str) -> None:
        self.prompt_calls.append((session_id, message))
        if self.prompt_error:
            raise self.prompt_error

    def get_state(self, session_id: str) -> dict:
        if self.state_error:
            raise self.state_error
        if self.open_result.session_path:
            return {"sessionFile": self.open_result.session_path}
        return {}

    def abort(self, session_id: str) -> None:
        self.aborted.append(session_id)
        if self.abort_error:
            raise self.abort_error

    def close_session(self, session_id: str) -> bool:
        self.closed_sessions.append(session_id)
        return True

    def collect(self, timeout_s: float, buffered=None):
        self.collect_timeouts.append(timeout_s)
        if self.clock is not None and self.collect_advance:
            self.clock.advance(self.collect_advance.pop(0))
        if self.collect_results:
            result = self.collect_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return "running", None, ""


class HandlerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = SessionStore(Path(self._tmp.name) / "data.db")
        self.addCleanup(self.store.close)
        store_patch = patch.object(tools, "_STORE", self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        ctx_patch = patch.object(tools, "_CTX", None)
        ctx_patch.start()
        self.addCleanup(ctx_patch.stop)

    def add_session(self, status="running", session_path="/tmp/omo-session.json",
                    omo_session_id="rpc-1", durable_session_id="durable-1"):
        self.store.create(id="omo-test", hermes_session_id="hermes-1",
                          omo_session_id=omo_session_id,
                          durable_session_id=durable_session_id,
                          session_path=session_path, cwd=self._tmp.name,
                          model="provider/model", permission="full-access",
                          status=status)
        return self.store.get("omo-test")

    def start(self, args, host, *, check_protocol=True):
        patches = [
            patch.object(tools, "ensure_host"),
            patch.object(tools, "HostClient", return_value=host),
            patch.object(tools, "_open_host", return_value=host),
        ]
        if check_protocol:
            patches.append(patch.object(tools, "check_protocol"))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return json.loads(tools.handle_start(args, session_id="hermes-1"))


class TestHandleStart(HandlerTestCase):
    def test_missing_task_does_not_open_a_host_session(self):
        host = FakeHost()
        with patch.object(tools, "ensure_host") as ensure, \
                patch.object(tools, "HostClient", return_value=host) as client:
            out = json.loads(tools.handle_start({"cwd": self._tmp.name},
                                                session_id="hermes-1"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "omo_error")
        self.assertEqual(out["detail"], "task is required")
        client.assert_not_called()
        ensure.assert_not_called()
        self.assertEqual(host.open_calls, 0)
        self.assertEqual(self.store.list_recent("hermes-1"), [])

    def test_prompt_failure_closes_the_host_session(self):
        host = FakeHost(prompt_error=HostError("omo_error", "prompt rejected"))
        out = self.start({"task": "do it", "cwd": self._tmp.name}, host)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "omo_error")
        self.assertEqual(host.closed_sessions, ["rpc-1"])
        self.assertEqual(self.store.list_recent("hermes-1"), [])

    def test_missing_session_path_closes_and_reports_session_lost(self):
        host = FakeHost(open_result=OpenResult(
            session_id="rpc-1", session_path=None, durable_session_id="durable-1"))
        with patch.object(tools.time, "sleep"):
            out = self.start({"task": "do it", "cwd": self._tmp.name}, host)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "session_lost")
        self.assertEqual(out["detail"], "host did not return a session path")
        self.assertEqual(host.closed_sessions, ["rpc-1"])
        self.assertEqual(self.store.list_recent("hermes-1"), [])

    def test_get_state_failure_closes_the_host_session(self):
        host = FakeHost(open_result=OpenResult(
            session_id="rpc-1", session_path=None, durable_session_id=None),
            state_error=HostError("host_unavailable", "gone"))
        out = self.start({"task": "do it", "cwd": self._tmp.name}, host)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "host_unavailable")
        self.assertEqual(host.closed_sessions, ["rpc-1"])
        self.assertEqual(self.store.list_recent("hermes-1"), [])

    def test_store_failure_closes_the_host_session(self):
        host = FakeHost()
        with patch.object(self.store, "create", side_effect=RuntimeError("db down")):
            out = self.start({"task": "do it", "cwd": self._tmp.name}, host)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "omo_error")
        self.assertEqual(host.closed_sessions, ["rpc-1"])
        self.assertEqual(self.store.list_recent("hermes-1"), [])

    def test_success_records_the_session(self):
        host = FakeHost()
        out = self.start({"task": "do it", "cwd": self._tmp.name}, host)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "running")
        rec = self.store.get(out["session_id"])
        self.assertEqual(rec["session_path"], "/tmp/omo-session.json")
        self.assertEqual(rec["status"], "running")
        self.assertEqual(host.prompt_calls, [("rpc-1", "do it")])

    def test_rejects_a_second_session_unless_parallel(self):
        self.add_session(status="running")
        host = FakeHost()
        with patch.object(tools, "ensure_host") as ensure, \
                patch.object(tools, "HostClient", return_value=host) as client:
            out = json.loads(tools.handle_start({"task": "again"},
                                                session_id="hermes-1"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "session_running")
        client.assert_not_called()
        ensure.assert_not_called()


class TestHandleWait(HandlerTestCase):
    def test_retry_shares_one_timeout_deadline(self):
        rec = self.add_session()
        clock = FakeClock()
        host = FakeHost(clock=clock, collect_advance=[80, 10],
                        collect_results=[TransportGone(), TransportGone()])
        with patch.object(tools, "_open_host", return_value=host), \
                patch.object(tools.time, "time", clock.time):
            out = json.loads(tools.handle_wait(
                {"session_id": rec["id"], "timeout_s": 90}, session_id="hermes-1"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "running")
        self.assertEqual(host.collect_timeouts, [90.0, 10.0])
        self.assertEqual(clock.now - 1000.0, 90.0)
        self.assertEqual(self.store.get(rec["id"])["status"], "running")

    def test_stored_error_status_is_reported_as_an_error(self):
        rec = self.add_session(status="error")
        self.store.update(rec["id"], last_text="boom")
        out = json.loads(tools.handle_wait({"session_id": rec["id"]},
                                           session_id="hermes-1"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "omo_error")
        self.assertEqual(out["detail"], "boom")


class TestHandleStatus(HandlerTestCase):
    def test_exposes_recovery_ids(self):
        rec = self.add_session()
        with patch.object(tools, "host_status",
                          return_value={"reachable": True, "pid": 7,
                                        "generation": 2}):
            out = json.loads(tools.handle_status({"session_id": rec["id"]},
                                                 session_id="hermes-1"))
        self.assertEqual(out["session_path"], "/tmp/omo-session.json")
        self.assertEqual(out["OMO_SESSION_ID"], "rpc-1")
        self.assertEqual(out["durable_session_id"], "durable-1")


class TestHandleCancel(HandlerTestCase):
    def test_oserror_from_abort_still_marks_the_session_cancelled(self):
        rec = self.add_session()
        host = FakeHost(abort_error=OSError("broken pipe"))
        with patch.object(tools, "_open_host", return_value=host):
            out = json.loads(tools.handle_cancel({"session_id": rec["id"]},
                                                 session_id="hermes-1"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(self.store.get(rec["id"])["status"], "cancelled")
        self.assertEqual(host.closed_sessions, ["rpc-1"])

    def test_terminal_status_is_not_overwritten(self):
        rec = self.add_session(status="done")
        host = FakeHost()
        with patch.object(tools, "_open_host", return_value=host) as open_host:
            out = json.loads(tools.handle_cancel({"session_id": rec["id"]},
                                                 session_id="hermes-1"))
        self.assertEqual(out["status"], "done")
        self.assertEqual(self.store.get(rec["id"])["status"], "done")
        open_host.assert_not_called()


if __name__ == "__main__":
    unittest.main()
