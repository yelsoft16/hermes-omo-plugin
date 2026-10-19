import unittest

from omo.questions import build_answer, normalize_frame


QUESTION_FRAME = {
    "type": "extension_ui_request",
    "method": "question",
    "id": "frame-1",
    "requestId": "tool-1",
    "waitForAnswer": True,
    "remainingMs": 1800000,
    "questions": [{
        "id": "q1",
        "header": "Approval",
        "question": "Approve the plan?",
        "multiSelect": False,
        "options": [
            {"label": "Approve", "description": "Write the plan"},
            {"label": "Change approach", "description": "Revise first"},
        ],
    }],
}


class TestNormalizeFrame(unittest.TestCase):
    def test_question_frame(self):
        payload = normalize_frame(QUESTION_FRAME)
        self.assertEqual(payload["request_id"], "frame-1")
        self.assertEqual(payload["kind"], "question")
        self.assertEqual(payload["timeout_s"], 1800)
        self.assertEqual(payload["questions"][0]["id"], "q1")
        self.assertEqual(payload["questions"][0]["options"][0]["label"], "Approve")

    def test_select_fallback(self):
        payload = normalize_frame({
            "type": "extension_ui_request", "method": "select",
            "id": "frame-2", "title": "Which DB?",
            "options": ["PostgreSQL", "SQLite"],
        })
        self.assertEqual(payload["kind"], "select")
        self.assertEqual(payload["questions"][0]["options"][0]["label"], "PostgreSQL")

    def test_input_fallback(self):
        payload = normalize_frame({
            "type": "extension_ui_request", "method": "input",
            "id": "frame-3", "title": "Anything else?",
        })
        self.assertEqual(payload["kind"], "input")
        self.assertTrue(payload["questions"][0]["freeText"])

    def test_confirm_fallback(self):
        payload = normalize_frame({
            "type": "extension_ui_request", "method": "confirm",
            "id": "frame-4", "title": "Continue?",
        })
        self.assertEqual(payload["kind"], "confirm")
        self.assertEqual([o["label"] for o in payload["questions"][0]["options"]],
                         ["Yes", "No"])

    def test_ignores_fire_and_forget(self):
        self.assertIsNone(normalize_frame({"type": "extension_ui_request", "method": "setStatus"}))
        self.assertIsNone(normalize_frame({"type": "message_update"}))


class TestBuildAnswer(unittest.TestCase):
    def test_question_answer(self):
        payload = normalize_frame(QUESTION_FRAME)
        body = build_answer(payload, {"q1": {"selected": ["Approve"], "text": ""}})
        self.assertEqual(body, {"answers": {"q1": {"selected": ["Approve"], "text": ""}},
                                "comment": ""})

    def test_question_comment(self):
        payload = normalize_frame(QUESTION_FRAME)
        body = build_answer(payload, {}, comment="looks good")
        self.assertEqual(body["comment"], "looks good")

    def test_select_answer(self):
        payload = normalize_frame({
            "type": "extension_ui_request", "method": "select",
            "id": "frame-2", "title": "Which DB?",
            "options": ["PostgreSQL", "SQLite"],
        })
        self.assertEqual(build_answer(payload, {"frame-2": {"selected": ["SQLite"]}}),
                         {"value": "SQLite"})

    def test_input_answer(self):
        payload = normalize_frame({
            "type": "extension_ui_request", "method": "input",
            "id": "frame-3", "title": "Name?",
        })
        self.assertEqual(build_answer(payload, {"frame-3": {"text": "AKRIDA"}}),
                         {"value": "AKRIDA"})

    def test_confirm_answer(self):
        payload = normalize_frame({
            "type": "extension_ui_request", "method": "confirm",
            "id": "frame-4", "title": "Continue?",
        })
        self.assertEqual(build_answer(payload, {"frame-4": {"selected": ["Yes"]}}),
                         {"confirmed": True})
        self.assertEqual(build_answer(payload, {"frame-4": {"selected": ["No"]}}),
                         {"confirmed": False})


if __name__ == "__main__":
    unittest.main()
