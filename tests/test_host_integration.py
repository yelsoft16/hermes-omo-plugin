import os
import tempfile
import time
import unittest

from omo.host_client import HostClient, check_protocol, ensure_host
from omo.questions import build_answer, normalize_frame

PROMPT = (
    "Use your ask-user/question tool to ask me which color I prefer, with options "
    "Red and Blue. After I answer, reply with exactly: ANSWER=<color>."
)


def _answer_first_option(host, opened, frame):
    payload = normalize_frame(frame)
    question = payload["questions"][0]
    label = question["options"][0]["label"]
    body = build_answer(payload, {question["id"]: {"selected": [label]}})
    host.answer(opened.session_id, payload["request_id"], body)


@unittest.skipUnless(os.environ.get("OMO_INTEGRATION") == "1",
                     "set OMO_INTEGRATION=1 to run against the real omo host")
class TestHostIntegration(unittest.TestCase):
    def test_question_roundtrip(self):
        ensure_host()
        with tempfile.TemporaryDirectory() as cwd:
            with HostClient() as host:
                check_protocol(host.protocol_info())
                opened = host.open_session(cwd=cwd, permission="full-access",
                                           name="hermes-itest")
                try:
                    host.prompt(opened.session_id, PROMPT)
                    status, frame, _ = host.collect(90, buffered=opened.buffered)
                    self.assertEqual(status, "question")
                    _answer_first_option(host, opened, frame)
                    status, _, text = host.collect(90)
                    self.assertEqual(status, "done")
                    self.assertIn("ANSWER=", text)
                finally:
                    host.close_session(opened.session_id)

    def test_pending_question_replays_after_disconnect(self):
        ensure_host()
        with tempfile.TemporaryDirectory() as cwd:
            # Leaving the `with` block drops the transport, not the session:
            # open_session asks the host to retain it on disconnect.
            with HostClient() as host:
                check_protocol(host.protocol_info())
                opened = host.open_session(cwd=cwd, permission="full-access",
                                           name="hermes-replay")
                host.prompt(opened.session_id, PROMPT)
                status, frame, _ = host.collect(90, buffered=opened.buffered)
                self.assertEqual(status, "question")
                path = opened.session_path
            time.sleep(1.0)
            with HostClient() as host2:
                opened2 = host2.open_session(session_path=path)
                try:
                    pending = [e for e in (opened2.state.get("pendingQuestions") or [])
                               if e.get("method") == "question"]
                    self.assertTrue(pending, "pending question was not replayed")
                    _answer_first_option(host2, opened2, pending[0])
                    status, _, text = host2.collect(90)
                    self.assertEqual(status, "done")
                    self.assertIn("ANSWER=", text)
                finally:
                    host2.close_session(opened2.session_id)

    def test_resume_after_close(self):
        ensure_host()
        with tempfile.TemporaryDirectory() as cwd:
            with HostClient() as host:
                check_protocol(host.protocol_info())
                opened = host.open_session(cwd=cwd, permission="full-access",
                                           name="hermes-resume")
                host.prompt(opened.session_id, "Reply with exactly: FIRST_OK")
                status, _, text = host.collect(90)
                self.assertEqual(status, "done")
                self.assertIn("FIRST_OK", text)
                path = opened.session_path
                host.close_session(opened.session_id)
            time.sleep(0.5)
            with HostClient() as host2:
                resumed = host2.open_session(session_path=path)
                try:
                    self.assertEqual(resumed.session_path, path)
                    host2.prompt(resumed.session_id, "Reply with exactly: RESUME_OK")
                    status, _, text = host2.collect(90)
                    self.assertEqual(status, "done")
                    self.assertIn("RESUME_OK", text)
                finally:
                    host2.close_session(resumed.session_id)


if __name__ == "__main__":
    unittest.main()
