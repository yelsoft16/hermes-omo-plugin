import tempfile
import unittest
from pathlib import Path

from omo.sessions import SessionStore, new_session_id


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.tmp.name) / "data.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _create(self, hermes="h1", status="running"):
        sid = new_session_id()
        self.store.create(
            id=sid, hermes_session_id=hermes, omo_session_id="rpc-1",
            durable_session_id="durable-1", session_path="/tmp/s.jsonl",
            cwd="/tmp", model="omniroute/glm-5.3-flash",
            permission="full-access", status=status,
        )
        return sid

    def test_create_and_get(self):
        sid = self._create()
        row = self.store.get(sid)
        self.assertEqual(row["id"], sid)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["cwd"], "/tmp")

    def test_update_whitelisted_fields(self):
        sid = self._create()
        self.store.update(sid, status="done", last_text="hello")
        row = self.store.get(sid)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["last_text"], "hello")

    def test_update_rejects_unknown_field(self):
        sid = self._create()
        with self.assertRaises(KeyError):
            self.store.update(sid, id="hacked")

    def test_active_for_ignores_done(self):
        sid = self._create(status="done")
        self.assertIsNone(self.store.active_for("h1"))
        self.store.update(sid, status="running")
        self.assertEqual(self.store.active_for("h1")["id"], sid)

    def test_resolve_prefers_active_then_latest(self):
        first = self._create(status="done")
        second = self._create(status="running")
        self.assertEqual(self.store.resolve("h1")["id"], second)
        self.store.update(second, status="done")
        self.assertEqual(self.store.resolve("h1")["id"], second)
        self.assertEqual(self.store.resolve("h1", session_id=first)["id"], first)

    def test_list_recent(self):
        self._create()
        self._create()
        self.assertEqual(len(self.store.list_recent("h1", limit=10)), 2)


if __name__ == "__main__":
    unittest.main()
