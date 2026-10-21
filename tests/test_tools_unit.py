import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo import questions, tools
from omo.host_client import HostError, OpenResult, TransportGone
from omo.sessions import SessionStore


def ui_frame(request_id: str, *, title: str = "Pick", key: str = "id") -> dict:
    """A minimal extension_ui_request as the host emits it."""
    return {"type": "extension_ui_request", key: request_id, "method": "select",
            "title": title, "options": ["a", "b"]}


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

    def test_matches_request_accepts_id_or_ui_request_id(self):
        self.assertTrue(tools._matches_request({"id": "q1"}, "q1"))
        self.assertTrue(tools._matches_request({"uiRequestId": "q1"}, "q1"))
        self.assertFalse(tools._matches_request({"id": "q2"}, "q1"))
        self.assertFalse(tools._matches_request({"id": None}, None))


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
                 answer_error: Exception | None = None,
                 collect_results: list | None = None,
                 clock: FakeClock | None = None,
                 collect_advance: list[float] | None = None) -> None:
        self.open_result = open_result or OpenResult(
            session_id="rpc-1", session_path="/tmp/omo-session.json",
            durable_session_id="durable-1", state={}, buffered=[])
        self.prompt_error = prompt_error
        self.state_error = state_error
        self.abort_error = abort_error
        self.answer_error = answer_error
        self.collect_results = list(collect_results or [])
        self.clock = clock
        self.collect_advance = list(collect_advance or [])
        self.open_calls = 0
        self.open_kwargs: dict = {}
        self.prompt_calls: list[tuple[str, str]] = []
        self.answer_calls: list[tuple[str, str, dict]] = []
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

    def answer(self, session_id: str, ui_request_id: str, body: dict) -> None:
        self.answer_calls.append((session_id, ui_request_id, body))
        if self.answer_error:
            raise self.answer_error

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
                    omo_session_id="rpc-1", durable_session_id="durable-1",
                    session_id="omo-test", hermes_session_id="hermes-1",
                    title=None):
        self.store.create(id=session_id, hermes_session_id=hermes_session_id,
                          omo_session_id=omo_session_id,
                          durable_session_id=durable_session_id,
                          session_path=session_path, cwd=self._tmp.name,
                          model="provider/model", permission="full-access",
                          status=status, title=title)
        return self.store.get(session_id)

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

    def test_workflow_prefixes_the_task_with_the_slash_command(self):
        host = FakeHost()
        out = self.start({"task": "review the plan", "workflow": "ulw-plan",
                          "cwd": self._tmp.name}, host)
        self.assertTrue(out["ok"])
        self.assertEqual(host.prompt_calls, [("rpc-1", "/ulw-plan review the plan")])

    def test_workflow_does_not_double_prefix_an_existing_command(self):
        host = FakeHost()
        out = self.start({"task": "/ulw-plan review the plan", "workflow": "ulw-plan",
                          "cwd": self._tmp.name}, host)
        self.assertTrue(out["ok"])
        self.assertEqual(host.prompt_calls, [("rpc-1", "/ulw-plan review the plan")])

    def test_unknown_workflow_is_rejected_before_opening_a_session(self):
        host = FakeHost()
        with patch.object(tools, "ensure_host") as ensure, \
                patch.object(tools, "HostClient", return_value=host):
            out = json.loads(tools.handle_start(
                {"task": "do it", "workflow": "bogus", "cwd": self._tmp.name},
                session_id="hermes-1"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "omo_error")
        self.assertIn("unsupported workflow", out["detail"])
        self.assertEqual(host.open_calls, 0)
        ensure.assert_not_called()

    def test_success_records_the_session(self):
        host = FakeHost()
        out = self.start({"task": "do it", "cwd": self._tmp.name}, host)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "running")
        rec = self.store.get(out["session_id"])
        self.assertEqual(rec["session_path"], "/tmp/omo-session.json")
        self.assertEqual(rec["status"], "running")
        self.assertEqual(host.prompt_calls, [("rpc-1", "do it")])

    def test_success_stores_the_task_as_the_session_title(self):
        host = FakeHost()
        out = self.start({"task": "fix the finalize bug\nand add a test",
                          "cwd": self._tmp.name}, host)
        rec = self.store.get(out["session_id"])
        self.assertEqual(rec["title"], "fix the finalize bug and add a test")

    def test_long_titles_are_truncated(self):
        host = FakeHost()
        out = self.start({"task": "x" * 200, "cwd": self._tmp.name}, host)
        rec = self.store.get(out["session_id"])
        self.assertLessEqual(len(rec["title"]), 80)
        self.assertTrue(rec["title"].endswith("..."))

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

    def test_settled_keeps_the_session_open_by_default(self):
        rec = self.add_session()
        host = FakeHost(collect_results=[("done", None, "final answer")])
        with patch.object(tools, "_open_host", return_value=host):
            out = json.loads(tools.handle_wait(
                {"session_id": rec["id"], "timeout_s": 10}, session_id="hermes-1"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["text"], "final answer")
        self.assertEqual(host.closed_sessions, [])
        row = self.store.get(rec["id"])
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["last_text"], "final answer")

    def test_auto_close_true_still_closes_and_marks_done(self):
        rec = self.add_session()
        host = FakeHost(collect_results=[("done", None, "final answer")])
        ctx = type("Ctx", (), {"get_config": lambda self, key, default=None: True})
        with patch.object(tools, "_open_host", return_value=host), \
                patch.object(tools, "_CTX", ctx()):
            out = json.loads(tools.handle_wait(
                {"session_id": rec["id"], "timeout_s": 10}, session_id="hermes-1"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "done")
        self.assertEqual(host.closed_sessions, ["rpc-1"])
        self.assertEqual(self.store.get(rec["id"])["status"], "done")

    def test_wait_reconciles_a_settled_transcript_instead_of_staying_running(self):
        transcript = Path(self._tmp.name) / "settled.jsonl"
        transcript.write_text(
            json.dumps({"type": "message", "message": {
                "role": "assistant", "stopReason": "stop",
                "content": [{"type": "text", "text": "final answer"}]}}) + "\n" +
            json.dumps({"type": "custom", "customType": "senpi.hooks.stop-state",
                        "data": {"count": 0}}) + "\n",
            encoding="utf-8")
        rec = self.add_session(session_path=str(transcript))
        with patch.object(tools, "_collect_with_retry",
                          return_value=("running", None, "")):
            out = json.loads(tools.handle_wait(
                {"session_id": rec["id"], "timeout_s": 10}))
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["text"], "final answer")
        self.assertEqual(self.store.get(rec["id"])["status"], "idle")

    def test_wait_does_not_settle_while_a_tool_call_is_open(self):
        transcript = Path(self._tmp.name) / "tool.jsonl"
        transcript.write_text(
            json.dumps({"type": "message", "message": {
                "role": "assistant", "stopReason": "toolUse",
                "content": [{"type": "toolCall", "name": "bash"}]}}) + "\n" +
            json.dumps({"type": "custom", "customType": "senpi.hooks.stop-state",
                        "data": {"count": 0}}) + "\n",
            encoding="utf-8")
        rec = self.add_session(session_path=str(transcript))
        with patch.object(tools, "_collect_with_retry",
                          return_value=("running", None, "")):
            out = json.loads(tools.handle_wait(
                {"session_id": rec["id"], "timeout_s": 10}))
        self.assertEqual(out["status"], "running")
        self.assertEqual(self.store.get(rec["id"])["status"], "running")

    def test_wait_does_not_settle_when_stop_state_has_pending_hooks(self):
        transcript = Path(self._tmp.name) / "hooks.jsonl"
        transcript.write_text(
            json.dumps({"type": "message", "message": {
                "role": "assistant", "stopReason": "stop",
                "content": [{"type": "text", "text": "final"}]}}) + "\n" +
            json.dumps({"type": "custom", "customType": "senpi.hooks.stop-state",
                        "data": {"count": 1}}) + "\n",
            encoding="utf-8")
        rec = self.add_session(session_path=str(transcript))
        with patch.object(tools, "_collect_with_retry",
                          return_value=("running", None, "")):
            out = json.loads(tools.handle_wait(
                {"session_id": rec["id"], "timeout_s": 10}))
        self.assertEqual(out["status"], "running")

    def test_wait_re_presents_a_pending_question_without_asking_the_host(self):
        rec = self.add_session(status="question")
        payload = questions.normalize_frame(ui_frame("q1"))
        self.store.update(rec["id"], pending_request_id="q1",
                          pending_question=json.dumps(payload))
        with patch.object(tools, "_open_host") as open_host, \
                patch.object(tools, "_collect_with_retry") as collect:
            out = json.loads(tools.handle_wait({"session_id": rec["id"]}))
        self.assertEqual(out["status"], "question")
        self.assertEqual(out["question"]["request_id"], "q1")
        open_host.assert_not_called()
        collect.assert_not_called()
        row = self.store.get(rec["id"])
        self.assertEqual(row["status"], "question")
        self.assertEqual(row["pending_request_id"], "q1")

    def test_stored_error_status_is_reported_as_an_error(self):
        rec = self.add_session(status="error")
        self.store.update(rec["id"], last_text="boom")
        out = json.loads(tools.handle_wait({"session_id": rec["id"]},
                                           session_id="hermes-1"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "omo_error")
        self.assertEqual(out["detail"], "boom")


class TestHandleStatus(HandlerTestCase):
    def test_keeps_a_pending_question_even_when_the_transcript_looks_settled(self):
        transcript = Path(self._tmp.name) / "settled-question.jsonl"
        transcript.write_text(
            json.dumps({"type": "message", "message": {
                "role": "assistant", "stopReason": "stop",
                "content": [{"type": "text", "text": "answer?"}]}}) + "\n" +
            json.dumps({"type": "custom", "customType": "senpi.hooks.stop-state",
                        "data": {"count": 0}}) + "\n",
            encoding="utf-8")
        rec = self.add_session(status="question", session_path=str(transcript))
        payload = questions.normalize_frame(ui_frame("q1"))
        self.store.update(rec["id"], pending_request_id="q1",
                          pending_question=json.dumps(payload))
        with patch.object(tools, "host_status", return_value={"reachable": False}):
            out = json.loads(tools.handle_status({"session_id": rec["id"]}))
        self.assertEqual(out["status"], "question")
        self.assertEqual(out["pending_question"]["request_id"], "q1")

    def test_reconciles_a_settled_transcript_to_idle(self):
        transcript = Path(self._tmp.name) / "settled-status.jsonl"
        transcript.write_text(
            json.dumps({"type": "message", "message": {
                "role": "assistant", "stopReason": "stop",
                "content": [{"type": "text", "text": "final answer"}]}}) + "\n" +
            json.dumps({"type": "custom", "customType": "senpi.hooks.stop-state",
                        "data": {"count": 0}}) + "\n",
            encoding="utf-8")
        rec = self.add_session(session_path=str(transcript))
        with patch.object(tools, "host_status", return_value={"reachable": False}):
            out = json.loads(tools.handle_status({"session_id": rec["id"]},
                                                 session_id="hermes-1"))
        self.assertEqual(out["status"], "idle")
        self.assertEqual(out["last_text"], "final answer")
        self.assertEqual(self.store.get(rec["id"])["status"], "idle")

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


class TestHandleAnswer(HandlerTestCase):
    def add_question(self, rec, request_id="q1") -> dict:
        payload = questions.normalize_frame(ui_frame(request_id))
        self.store.update(rec["id"], status="question",
                          pending_request_id=request_id,
                          pending_question=json.dumps(payload))
        return payload

    def test_removes_only_the_answered_frame_and_sends_the_answer(self):
        rec = self.add_session()
        self.add_question(rec, "q1")
        q1 = ui_frame("q1", title="First")
        q9 = ui_frame("q9", title="Second")
        # Same question, wire shape from the spike: filtered via uiRequestId.
        pending_q1 = ui_frame("q1", title="First", key="uiRequestId")
        host = FakeHost(open_result=OpenResult(
            session_id="rpc-1", session_path="/tmp/omo-session.json",
            durable_session_id="durable-1",
            state={"pendingQuestions": [pending_q1]}, buffered=[q1, q9]),
            collect_results=[("question", q9, "next")])
        with patch.object(tools, "_open_host", return_value=host):
            out = json.loads(tools.handle_answer(
                {"request_id": "q1", "answers": {"q1": {"selected": ["a"]}}},
                session_id="hermes-1"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "question")
        self.assertEqual(out["question"]["request_id"], "q9")
        self.assertEqual(host.answer_calls, [("rpc-1", "q1", {"value": "a"})])
        self.assertEqual([e["id"] for e in host.open_result.buffered], ["q9"])
        self.assertEqual(host.open_result.state["pendingQuestions"], [])
        self.assertEqual(self.store.get(rec["id"])["pending_request_id"], "q9")

    def test_already_resolved_question_is_not_an_error(self):
        rec = self.add_session()
        self.add_question(rec)
        host = FakeHost(answer_error=HostError("question_already_resolved", "gone"),
                        collect_results=[("running", None, "")])
        with patch.object(tools, "_open_host", return_value=host):
            out = json.loads(tools.handle_answer(
                {"request_id": "q1", "answers": {"q1": {"selected": ["a"]}}},
                session_id="hermes-1"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "running")
        self.assertIsNone(self.store.get(rec["id"])["pending_question"])

    def test_clears_the_pending_question_before_collecting(self):
        rec = self.add_session()
        self.add_question(rec)
        host = FakeHost(collect_results=[TransportGone()])
        with patch.object(tools, "_open_host", return_value=host):
            out = json.loads(tools.handle_answer(
                {"request_id": "q1", "answers": {}}, session_id="hermes-1"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "host_unavailable")
        row = self.store.get(rec["id"])
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["pending_request_id"])
        self.assertIsNone(row["pending_question"])

    def test_missing_question_frame_is_reported(self):
        rec = self.add_session()
        self.add_question(rec)
        host = FakeHost(collect_results=[("question", None, "")])
        with patch.object(tools, "_open_host", return_value=host):
            out = json.loads(tools.handle_answer(
                {"request_id": "q1", "answers": {}}, session_id="hermes-1"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "omo_error")
        self.assertEqual(out["detail"], "question event missing")


class TestOpenHost(unittest.TestCase):
    def test_ensures_the_host_before_connecting(self):
        class Stub:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs
                self.connected = False

            def connect(self):
                self.connected = True

        client = Stub()
        with patch.object(tools, "_socket_path", return_value="/tmp/omo.sock"), \
                patch.object(tools, "ensure_host") as ensure, \
                patch.object(tools, "HostClient", return_value=client) as factory:
            host = tools._open_host()
        self.assertIs(host, client)
        self.assertTrue(client.connected)
        ensure.assert_called_once_with("/tmp/omo.sock", timeout=90)
        factory.assert_called_once_with("/tmp/omo.sock", connect_timeout=10)


class TestHandleResume(HandlerTestCase):
    def test_requires_an_explicit_session_id(self):
        with patch.object(tools, "_open_host") as open_host:
            out = json.loads(tools.handle_resume({}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "omo_error")
        self.assertEqual(out["detail"], "session_id is required")
        open_host.assert_not_called()

    def test_rejects_a_message_while_still_live(self):
        for status in ("running", "question"):
            with self.subTest(status=status):
                rec = self.add_session(status=status, session_id=f"omo-{status}")
                with patch.object(tools, "_open_host") as open_host:
                    out = json.loads(tools.handle_resume(
                        {"session_id": rec["id"], "message": "again"}))
                self.assertFalse(out["ok"])
                self.assertEqual(out["error"], "session_running")
                open_host.assert_not_called()

    def test_no_message_resume_of_a_done_session_returns_idle(self):
        rec = self.add_session(status="done")
        host = FakeHost(open_result=OpenResult(
            session_id="rpc-2", session_path="/tmp/omo-session.json",
            durable_session_id=None))
        with patch.object(tools, "_open_host", return_value=host):
            out = json.loads(tools.handle_resume({"session_id": rec["id"]}))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "idle")
        self.assertEqual(host.open_calls, 1)
        self.assertEqual(host.prompt_calls, [])
        row = self.store.get(rec["id"])
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["omo_session_id"], "rpc-2")
        self.assertEqual(row["durable_session_id"], "durable-1")

    def test_no_message_resume_of_a_running_session_returns_running(self):
        rec = self.add_session(status="running")
        with patch.object(tools, "_open_host") as open_host:
            out = json.loads(tools.handle_resume({"session_id": rec["id"]}))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "running")
        self.assertIsNone(out["question"])
        open_host.assert_not_called()
        self.assertEqual(self.store.get(rec["id"])["status"], "running")

    def test_no_message_resume_keeps_a_pending_question(self):
        rec = self.add_session(status="question")
        payload = questions.normalize_frame(ui_frame("q1"))
        self.store.update(rec["id"], pending_request_id="q1",
                          pending_question=json.dumps(payload), last_text="working")
        with patch.object(tools, "_open_host") as open_host:
            out = json.loads(tools.handle_resume({"session_id": rec["id"]}))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "question")
        self.assertEqual(out["question"]["request_id"], "q1")
        open_host.assert_not_called()
        row = self.store.get(rec["id"])
        self.assertEqual(row["status"], "question")
        self.assertEqual(row["pending_request_id"], "q1")

    def test_prompt_failure_closes_the_reopened_session(self):
        rec = self.add_session(status="done")
        host = FakeHost(prompt_error=HostError("omo_error", "prompt rejected"))
        with patch.object(tools, "_open_host", return_value=host):
            out = json.loads(tools.handle_resume(
                {"session_id": rec["id"], "message": "go"}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "omo_error")
        self.assertEqual(host.closed_sessions, ["rpc-1"])
        self.assertEqual(self.store.get(rec["id"])["status"], "done")


class TestSessionStoreSchema(unittest.TestCase):
    def test_old_database_gets_a_title_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE sessions (
                  id TEXT PRIMARY KEY,
                  hermes_session_id TEXT,
                  omo_session_id TEXT,
                  durable_session_id TEXT,
                  session_path TEXT,
                  cwd TEXT NOT NULL,
                  model TEXT,
                  permission TEXT NOT NULL,
                  status TEXT NOT NULL,
                  pending_request_id TEXT,
                  pending_question TEXT,
                  last_text TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX idx_sessions_hermes
                  ON sessions (hermes_session_id, created_at DESC);
            """)
            conn.commit()
            conn.close()
            store = SessionStore(path)
            self.addCleanup(store.close)
            cols = {row[1] for row in store.conn.execute("PRAGMA table_info(sessions)")}
            self.assertIn("title", cols)


if __name__ == "__main__":
    unittest.main()
