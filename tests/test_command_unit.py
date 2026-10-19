import unittest
from unittest.mock import patch

from omo import tools
from tests.test_tools_unit import FakeHost, HandlerTestCase


class TestCommandUsage(unittest.TestCase):
    def test_help(self):
        self.assertIn("/omo status", tools.handle_command("help"))

    def test_unknown_subcommand(self):
        self.assertIn("Usage:", tools.handle_command("nonsense"))


class TestCommandSessions(HandlerTestCase):
    def test_status_without_kwargs_uses_the_latest_session(self):
        self.add_session(session_id="omo-old", status="done")
        self.add_session(session_id="omo-new", status="running")
        with patch.object(tools, "host_status",
                          return_value={"reachable": True, "pid": 7,
                                        "generation": 1}):
            out = tools.handle_command("status")
        self.assertIn("omo-new", out)
        self.assertIn("running", out)
        self.assertNotIn("omo-old", out)

    def test_stop_without_kwargs_cancels_the_latest_session(self):
        self.add_session(session_id="omo-old", status="running")
        self.add_session(session_id="omo-new", status="running")
        host = FakeHost()
        with patch.object(tools, "_open_host", return_value=host):
            out = tools.handle_command("stop")
        self.assertEqual(out, "omo-new: cancelled")
        self.assertEqual(self.store.get("omo-new")["status"], "cancelled")
        self.assertEqual(self.store.get("omo-old")["status"], "running")

    def test_list_shows_recent_sessions_newest_first(self):
        self.add_session(session_id="omo-old", status="done")
        self.add_session(session_id="omo-new", status="running")
        out = tools.handle_command("list")
        self.assertIn("omo-old", out)
        self.assertIn("omo-new", out)
        self.assertLess(out.index("omo-new"), out.index("omo-old"))

    def test_status_without_any_session_says_so(self):
        self.assertEqual(tools.handle_command("status"), "No omo sessions.")

    def test_empty_args_default_to_status(self):
        self.assertEqual(tools.handle_command(""), "No omo sessions.")

    def test_status_honours_a_bound_session_id(self):
        self.add_session(session_id="omo-mine", hermes_session_id="hermes-2",
                         status="done")
        self.add_session(session_id="omo-newer", hermes_session_id="hermes-1",
                         status="running")
        with patch.object(tools, "host_status", return_value={"reachable": False}):
            out = tools.handle_command("status", session_id="hermes-2")
        self.assertIn("omo-mine", out)
        self.assertNotIn("omo-newer", out)

    def test_list_scopes_to_a_bound_session(self):
        self.add_session(session_id="omo-mine", hermes_session_id="hermes-2")
        self.add_session(session_id="omo-other", hermes_session_id="hermes-1")
        out = tools.handle_command("list", session_id="hermes-2")
        self.assertIn("omo-mine", out)
        self.assertNotIn("omo-other", out)


class TestFormatStatus(unittest.TestCase):
    def test_tolerates_a_missing_question_list(self):
        text = tools._format_status({"session_id": "omo-1", "status": "question",
                                     "pending_question": {"questions": []}})
        self.assertNotIn("Pending:", text)

    def test_renders_the_first_question(self):
        text = tools._format_status({
            "session_id": "omo-1", "status": "question",
            "pending_question": {"questions": [
                {"header": "Pick", "question": "Which one?"}]}})
        self.assertIn("Pending: Pick — Which one?", text)


class _NoSkillPath:
    """Path stub whose bundled SKILL.md never exists."""

    def __init__(self, *args, **kwargs):
        pass

    @property
    def parent(self):
        return self

    def __truediv__(self, other):
        return self

    def exists(self) -> bool:
        return False


class RecordingContext:
    def __init__(self) -> None:
        self.tools: list[dict] = []
        self.commands: list[tuple] = []
        self.skills: list[tuple] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_command(self, name, handler, **kwargs) -> None:
        self.commands.append((name, handler, kwargs))

    def register_skill(self, name, path) -> None:
        self.skills.append((name, path))


class TestRegister(unittest.TestCase):
    def test_registers_six_tools_the_command_and_skips_the_missing_skill(self):
        ctx = RecordingContext()
        with patch.object(tools, "_CTX", None), \
                patch.object(tools, "Path", _NoSkillPath):
            tools.register(ctx)
        self.assertEqual([t["name"] for t in ctx.tools],
                         [s["name"] for s in tools.schemas.ALL])
        self.assertEqual(len(ctx.tools), 6)
        self.assertTrue(all(t["check_fn"] is tools._check_requirements
                            for t in ctx.tools))
        self.assertEqual([c[0] for c in ctx.commands], ["omo"])
        self.assertIs(ctx.commands[0][1], tools.handle_command)
        self.assertEqual(ctx.commands[0][2]["args_hint"],
                         "<status|list|stop|resume>")
        self.assertEqual(ctx.skills, [])


if __name__ == "__main__":
    unittest.main()
