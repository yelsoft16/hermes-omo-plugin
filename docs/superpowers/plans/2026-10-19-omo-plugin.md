# Hermes–OmO RPC Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local Hermes plugin that starts OmO sessions, relays OmO's mid-turn questions to the user, sends answers back, and runs `/ulw-plan` end to end.

**Architecture:** The plugin is a thin, stateless client over OmO's own Unix-socket host daemon. Each tool call opens a socket, attaches to a session by its durable `sessionPath`, reads JSONL events, and detaches. Session metadata lives in a per-plugin SQLite database; the OmO host owns the live session and replays pending questions after reconnects. Questions are surfaced to the user with Hermes's native `clarify` tool.

**Tech Stack:** Python 3.11+ standard library only (`socket`, `select`, `json`, `sqlite3`, `subprocess`), Hermes plugin API (`ctx.register_tool`, `ctx.register_command`, `ctx.register_skill`), OmO 5.1.35 host RPC protocol v1.

**Spec:** `docs/specs/2026-10-19-omo-plugin-design.md`

## Global Constraints

- Plugin root is `~/.hermes/plugins/omo/`; all paths below are relative to it unless absolute.
- Python 3.11+; **no pip dependencies** — stdlib only.
- OmO 5.1.35, `protocolVersion == 1`; required host capabilities: `multi_session`, `retain_on_disconnect`.
- Tool names and error codes must match the spec exactly: `omo_start`, `omo_wait`, `omo_answer`, `omo_status`, `omo_cancel`, `omo_resume`; `host_unavailable`, `protocol_mismatch`, `invalid_cwd`, `invalid_permission`, `session_running`, `session_not_found`, `session_lost`, `question_already_resolved`, `timeout`, `omo_error`.
- Every tool handler returns a JSON string and never raises.
- `session_path` must be read from `data.state.sessionFile`; `sessionId` is required on every `extension_ui_response`; a replayed question frame may arrive before the `open_session` response.
- Defaults: `default_cwd=/home/frappe/frappe-bench`, `permission_default=full-access`, `wait_timeout_s=90` (max 120), `auto_close=true`.
- Code comments are ASCII-only. No edits to Hermes core.
- Commit after every task with the message shown in that task.

---

### Task 1: Plugin skeleton and JSONL framing

**Files:**
- Create: `plugin.yaml`
- Create: `__init__.py`
- Create: `host_client.py`
- Create: `README.md`
- Create: `tests/__init__.py`
- Create: `tests/test_framing.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `host_client.JsonlBuffer.feed(chunk: bytes) -> list[dict]`.

- [ ] **Step 1: Write the failing framing test**

`tests/__init__.py`:

```python
import sys
from pathlib import Path

# ~/.hermes/plugins must be on sys.path so `import omo` resolves the plugin package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
```

`tests/test_framing.py`:

```python
import unittest

from omo.host_client import JsonlBuffer


class TestJsonlBuffer(unittest.TestCase):
    def test_single_complete_line(self):
        buf = JsonlBuffer()
        self.assertEqual(buf.feed(b'{"a": 1}\n'), [{"a": 1}])

    def test_split_line(self):
        buf = JsonlBuffer()
        self.assertEqual(buf.feed(b'{"a"'), [])
        self.assertEqual(buf.feed(b': 1}\n'), [{"a": 1}])

    def test_multiple_lines_in_one_chunk(self):
        buf = JsonlBuffer()
        self.assertEqual(buf.feed(b'{"a": 1}\n{"b": 2}\n'), [{"a": 1}, {"b": 2}])

    def test_multibyte_character_split_across_chunks(self):
        data = '{"t": "çözgü"}\n'.encode("utf-8")
        buf = JsonlBuffer()
        out = []
        for i in range(len(data)):
            out.extend(buf.feed(data[i:i + 1]))
        self.assertEqual(out, [{"t": "çözgü"}])

    def test_blank_lines_are_ignored(self):
        buf = JsonlBuffer()
        self.assertEqual(buf.feed(b'\n\n{"a": 1}\n\n'), [{"a": 1}])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m unittest tests.test_framing -v`
Expected: `ModuleNotFoundError: No module named 'omo'` (package does not exist yet).

- [ ] **Step 3: Create the plugin skeleton**

`plugin.yaml`:

```yaml
name: omo
version: 0.1.0
description: "OmO (oh-my-openagent) RPC bridge: interactive omo sessions from Hermes."
author: Selçuk ÇALIŞKAN
requires_hermes: ">=0.21.5"
provides_tools: []
provides_hooks: []
```

`__init__.py`:

```python
"""Hermes-OmO RPC bridge plugin."""


def register(ctx):
    """Plugin entry point. Tools are wired in Task 5."""
    return None
```

`host_client.py`:

```python
"""OmO host RPC client. Hermes-agnostic: socket, JSONL framing, protocol."""

from __future__ import annotations

import json


class JsonlBuffer:
    """Incremental JSONL decoder safe across arbitrary chunk boundaries."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> list[dict]:
        self._buf += chunk
        out: list[dict] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line.decode("utf-8")))
        return out
```

`README.md`:

```markdown
# omo — Hermes plugin

Local Hermes plugin that drives OmO (oh-my-openagent) sessions over its RPC host.

- Start a new session: `omo_start`
- Wait for a question or completion: `omo_wait`
- Answer a question: `omo_answer`
- Control: `omo_status`, `omo_cancel`, `omo_resume`
- Slash command: `/omo status|list|stop|resume <id>`

Design: `docs/specs/2026-10-19-omo-plugin-design.md`
```

- [ ] **Step 4: Run the test and verify it passes**

Run: `python3 -m unittest tests.test_framing -v`
Expected: 5 tests, `OK`.

- [ ] **Step 5: Validate the plugin manifest**

Run: `hermes plugins doctor ~/.hermes/plugins/omo`
Expected: no error; an empty `provides_tools` list is accepted at this stage.

- [ ] **Step 6: Commit**

```bash
git add plugin.yaml __init__.py host_client.py README.md tests/
git commit -m "feat: add omo plugin skeleton and jsonl framing"
```

---

### Task 2: Question frame normalization and answer mapping

**Files:**
- Create: `questions.py`
- Create: `tests/test_questions.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `questions.normalize_frame(frame: dict) -> dict | None`
  - `questions.build_answer(payload: dict, answers: dict, comment: str = "") -> dict`

- [ ] **Step 1: Write the failing tests**

`tests/test_questions.py`:

```python
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
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python3 -m unittest tests.test_questions -v`
Expected: `ModuleNotFoundError: No module named 'omo.questions'`.

- [ ] **Step 3: Implement `questions.py`**

```python
"""Normalize OmO extension UI frames into one question payload shape."""

from __future__ import annotations


def _label(option) -> str:
    if isinstance(option, dict):
        return option.get("label", "")
    return str(option)


def _single(frame: dict, kind: str, title: str, options: list[dict], free_text: bool) -> dict:
    return {
        "request_id": frame.get("id"),
        "kind": kind,
        "questions": [{
            "id": frame.get("id"),
            "header": title,
            "question": title,
            "multiSelect": False,
            "freeText": free_text,
            "options": options,
        }],
        "timeout_s": max(0, int((frame.get("remainingMs") or 0) / 1000)),
    }


def normalize_frame(frame: dict) -> dict | None:
    method = frame.get("method")
    if method == "question":
        return {
            "request_id": frame.get("id"),
            "kind": "question",
            "questions": [{
                "id": q.get("id"),
                "header": q.get("header") or q.get("question") or "Question",
                "question": q.get("question") or "",
                "multiSelect": bool(q.get("multiSelect")),
                "freeText": False,
                "options": [
                    {"label": o.get("label", ""), "description": o.get("description", "")}
                    for o in (q.get("options") or [])
                ],
            } for q in (frame.get("questions") or [])],
            "timeout_s": max(0, int((frame.get("remainingMs") or 0) / 1000)),
        }
    if method == "select":
        return _single(
            frame, "select", frame.get("title") or "Select",
            [{"label": _label(o), "description": ""} for o in (frame.get("options") or [])],
            False,
        )
    if method == "input":
        return _single(frame, "input", frame.get("title") or "Input", [], True)
    if method == "confirm":
        return _single(
            frame, "confirm", frame.get("title") or "Confirm",
            [{"label": "Yes", "description": ""}, {"label": "No", "description": ""}],
            False,
        )
    return None


def _first_selected(payload: dict, answers: dict) -> str:
    for q in payload.get("questions", []):
        entry = (answers or {}).get(q["id"]) or {}
        selected = entry.get("selected") or []
        if selected:
            return selected[0]
    return ""


def _first_text(payload: dict, answers: dict) -> str:
    for q in payload.get("questions", []):
        entry = (answers or {}).get(q["id"]) or {}
        text = entry.get("text") or ""
        if text:
            return text
    return ""


def build_answer(payload: dict, answers: dict, comment: str = "") -> dict:
    kind = payload.get("kind")
    if kind == "question":
        return {"answers": answers or {}, "comment": comment or ""}
    if kind == "select":
        return {"value": _first_selected(payload, answers)}
    if kind == "input":
        return {"value": _first_text(payload, answers)}
    if kind == "confirm":
        return {"confirmed": _first_selected(payload, answers).lower() in ("yes", "true", "1")}
    raise ValueError(f"unsupported question kind: {kind}")
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python3 -m unittest tests.test_questions -v`
Expected: 10 tests, `OK`.

- [ ] **Step 5: Commit**

```bash
git add questions.py tests/test_questions.py
git commit -m "feat: normalize omo question frames"
```

---

### Task 3: Session store

**Files:**
- Create: `sessions.py`
- Create: `tests/test_sessions.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `sessions.new_session_id() -> str`
  - `sessions.SessionStore(db_path)` with `create`, `get`, `update`, `active_for`, `resolve`, `list_recent`.

- [ ] **Step 1: Write the failing tests**

`tests/test_sessions.py`:

```python
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
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python3 -m unittest tests.test_sessions -v`
Expected: `ModuleNotFoundError: No module named 'omo.sessions'`.

- [ ] **Step 3: Implement `sessions.py`**

```python
"""Durable plugin session records in SQLite."""

from __future__ import annotations

import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id                 TEXT PRIMARY KEY,
  hermes_session_id  TEXT,
  omo_session_id     TEXT,
  durable_session_id TEXT,
  session_path       TEXT,
  cwd                TEXT NOT NULL,
  model              TEXT,
  permission         TEXT NOT NULL,
  status             TEXT NOT NULL,
  pending_request_id TEXT,
  pending_question   TEXT,
  last_text          TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_hermes
  ON sessions (hermes_session_id, created_at DESC);
"""

UPDATABLE = {
    "omo_session_id", "durable_session_id", "session_path", "cwd", "model",
    "permission", "status", "pending_request_id", "pending_question", "last_text",
}

ACTIVE_STATUSES = ("starting", "running", "question")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_id() -> str:
    return f"omo-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


class SessionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def create(self, *, id: str, hermes_session_id: str, omo_session_id: str | None,
               durable_session_id: str | None, session_path: str | None, cwd: str,
               model: str | None, permission: str, status: str = "running") -> None:
        ts = now_iso()
        with self._lock:
            self.conn.execute(
                """INSERT INTO sessions
                   (id, hermes_session_id, omo_session_id, durable_session_id, session_path,
                    cwd, model, permission, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (id, hermes_session_id, omo_session_id, durable_session_id, session_path,
                 cwd, model, permission, status, ts, ts),
            )
            self.conn.commit()

    def get(self, session_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()

    def update(self, session_id: str, **fields) -> None:
        unknown = set(fields) - UPDATABLE
        if unknown:
            raise KeyError(sorted(unknown))
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [session_id]
        with self._lock:
            self.conn.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", values)
            self.conn.commit()

    def active_for(self, hermes_session_id: str) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        return self.conn.execute(
            f"""SELECT * FROM sessions
                WHERE hermes_session_id = ? AND status IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1""",
            (hermes_session_id, *ACTIVE_STATUSES),
        ).fetchone()

    def resolve(self, hermes_session_id: str, session_id: str | None = None) -> sqlite3.Row | None:
        if session_id:
            return self.get(session_id)
        row = self.conn.execute(
            """SELECT * FROM sessions
               WHERE hermes_session_id = ?
                 AND status IN ('starting','idle','running','question')
               ORDER BY created_at DESC LIMIT 1""",
            (hermes_session_id,),
        ).fetchone()
        if row:
            return row
        return self.conn.execute(
            """SELECT * FROM sessions WHERE hermes_session_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (hermes_session_id,),
        ).fetchone()

    def list_recent(self, hermes_session_id: str, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM sessions WHERE hermes_session_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (hermes_session_id, limit),
        ).fetchall()
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python3 -m unittest tests.test_sessions -v`
Expected: 6 tests, `OK`.

- [ ] **Step 5: Commit**

```bash
git add sessions.py tests/test_sessions.py
git commit -m "feat: add durable session store"
```

---

### Task 4: Host client — socket, protocol, open/prompt/collect/answer/close

**Files:**
- Modify: `host_client.py`
- Create: `tests/test_host_client_unit.py`

**Interfaces:**
- Consumes: `JsonlBuffer` from Task 1.
- Produces:
  - `default_socket_path() -> str`
  - `host_status(socket_path: str | None = None) -> dict`
  - `ensure_host(socket_path: str | None = None, timeout: int = 30) -> dict`
  - `check_protocol(info: dict) -> None`
  - `parse_open_response(resp: dict, fallback_path: str | None = None) -> OpenResult`
  - `HostClient(socket_path=None)` with `connect`, `close`, `protocol_info`, `open_session`, `prompt`, `get_state`, `answer`, `collect`, `abort`, `close_session`.

- [ ] **Step 1: Write the failing unit tests**

`tests/test_host_client_unit.py`:

```python
import unittest

from omo.host_client import (
    HostError,
    OpenResult,
    check_protocol,
    default_socket_path,
    parse_open_response,
)


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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python3 -m unittest tests.test_host_client_unit -v`
Expected: `ImportError` for `HostError`, `OpenResult`, `check_protocol`, etc.

- [ ] **Step 3: Implement the host client**

Append to `host_client.py` (replace the file with the full version below):

```python
"""OmO host RPC client. Hermes-agnostic: socket, JSONL framing, protocol."""

from __future__ import annotations

import json
import os
import select
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field

DIALOG_METHODS = {"question", "select", "input", "confirm"}


class HostError(Exception):
    def __init__(self, code: str, detail: str = "", hint: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.hint = hint


class TransportGone(HostError):
    def __init__(self, detail: str = "host connection closed") -> None:
        super().__init__("host_unavailable", detail)


class JsonlBuffer:
    """Incremental JSONL decoder safe across arbitrary chunk boundaries."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> list[dict]:
        self._buf += chunk
        out: list[dict] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line.decode("utf-8")))
        return out


@dataclass
class OpenResult:
    session_id: str
    session_path: str | None
    durable_session_id: str | None
    state: dict = field(default_factory=dict)
    buffered: list[dict] = field(default_factory=list)


def default_socket_path() -> str:
    return os.path.expanduser("~/.omo/agent/rpc/rpc.sock")


def _last_json_line(text: str) -> dict:
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError("no JSON object in output")


def host_status(socket_path: str | None = None, timeout: int = 15) -> dict:
    omo = shutil.which("omo")
    if not omo:
        raise HostError("host_unavailable", "omo binary not found", "Install omo")
    args = [omo, "host", "status", "--json"]
    if socket_path:
        args += ["--socket", socket_path]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise HostError("host_unavailable", proc.stderr.strip() or proc.stdout.strip(),
                        "Run: omo host ensure")
    return _last_json_line(proc.stdout)


def ensure_host(socket_path: str | None = None, timeout: int = 30) -> dict:
    status = host_status(socket_path, timeout=timeout)
    if status.get("reachable"):
        return status
    omo = shutil.which("omo")
    if not omo:
        raise HostError("host_unavailable", "omo binary not found", "Install omo")
    args = [omo, "host", "ensure", "--json"]
    if socket_path:
        args += ["--socket", socket_path]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise HostError("host_unavailable", proc.stderr.strip() or proc.stdout.strip(),
                        "Run: omo host ensure")
    return _last_json_line(proc.stdout)


def check_protocol(info: dict) -> None:
    if int(info.get("protocolVersion") or 0) != 1:
        raise HostError("protocol_mismatch",
                        f"protocolVersion={info.get('protocolVersion')}",
                        "Upgrade or pin the omo runtime")
    required = {"multi_session", "retain_on_disconnect"}
    missing = required - set(info.get("capabilities") or [])
    if missing:
        raise HostError("protocol_mismatch", f"missing capabilities: {sorted(missing)}",
                        "Upgrade or pin the omo runtime")


def parse_open_response(resp: dict, fallback_path: str | None = None) -> OpenResult:
    data = resp.get("data") or {}
    state = data.get("state") or {}
    return OpenResult(
        session_id=data.get("sessionId") or "",
        session_path=state.get("sessionFile") or fallback_path,
        durable_session_id=state.get("sessionId"),
        state=state,
    )


class HostClient:
    def __init__(self, socket_path: str | None = None, connect_timeout: float = 5.0) -> None:
        self.socket_path = socket_path or default_socket_path()
        self._connect_timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._buf = JsonlBuffer()
        self._seq = 0

    def __enter__(self) -> "HostClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._connect_timeout)
        try:
            sock.connect(self.socket_path)
        except OSError as exc:
            sock.close()
            raise HostError("host_unavailable", str(exc), "Run: omo host ensure") from exc
        sock.settimeout(None)
        self._sock = sock
        self._buf = JsonlBuffer()
        self._send({"id": "ci", "type": "set_client_info", "width": 100,
                    "capabilities": ["question"]})

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def _send(self, obj: dict) -> None:
        if self._sock is None:
            raise HostError("host_unavailable", "not connected")
        self._sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    def _events(self, deadline: float):
        while time.time() < deadline:
            if self._sock is None:
                raise TransportGone()
            try:
                ready, _, _ = select.select([self._sock], [], [], 0.25)
            except (OSError, ValueError) as exc:
                raise TransportGone(str(exc)) from exc
            if not ready:
                yield None
                continue
            try:
                chunk = self._sock.recv(65536)
            except OSError as exc:
                raise TransportGone(str(exc)) from exc
            if not chunk:
                raise TransportGone()
            for event in self._buf.feed(chunk):
                yield event

    def request(self, payload: dict, timeout: float = 15.0) -> tuple[dict, list[dict]]:
        payload = dict(payload)
        payload["id"] = payload.get("id") or self._next_id(payload["type"])
        rid = payload["id"]
        self._send(payload)
        extra: list[dict] = []
        deadline = time.time() + timeout
        for event in self._events(deadline):
            if event is None:
                continue
            if event.get("type") == "response" and event.get("id") == rid:
                if not event.get("success"):
                    err = event.get("error") or ""
                    raise HostError(event.get("errorCode") or err or "omo_error", err, "")
                return event, extra
            extra.append(event)
        raise HostError("timeout", f"no response for {payload['type']}")

    def protocol_info(self) -> dict:
        resp, _ = self.request({"type": "get_protocol_info"}, timeout=10)
        return resp.get("data") or {}

    def open_session(self, *, cwd: str | None = None, session_path: str | None = None,
                     name: str | None = None, permission: str | None = None,
                     provider: str | None = None, model_id: str | None = None) -> OpenResult:
        payload: dict = {"type": "open_session", "retain_on_disconnect": True}
        if session_path:
            payload["sessionPath"] = session_path
        else:
            payload["promptSurface"] = "terminal"
        if cwd:
            payload["cwd"] = cwd
        if name:
            payload["name"] = name
        if permission:
            payload["permissionPreset"] = permission
        if provider:
            payload["provider"] = provider
        if model_id:
            payload["modelId"] = model_id
        resp, extra = self.request(payload, timeout=20)
        opened = parse_open_response(resp, fallback_path=session_path)
        opened.buffered = [e for e in extra if e.get("type") == "extension_ui_request"]
        return opened

    def prompt(self, session_id: str, message: str) -> str:
        resp, _ = self.request({"type": "prompt", "sessionId": session_id,
                                "message": message}, timeout=20)
        return (resp.get("data") or {}).get("disposition", "")

    def get_state(self, session_id: str) -> dict:
        resp, _ = self.request({"type": "get_state", "sessionId": session_id}, timeout=10)
        return (resp.get("data") or {}).get("state") or {}

    def answer(self, session_id: str, ui_request_id: str, body: dict) -> None:
        payload = {"type": "extension_ui_response", "sessionId": session_id,
                   "uiRequestId": ui_request_id}
        payload.update(body)
        self.request(payload, timeout=15)

    def collect(self, timeout_s: float = 90, buffered: list[dict] | None = None
                ) -> tuple[str, dict | None, str]:
        deadline = time.time() + max(1.0, min(float(timeout_s), 120.0))
        text: list[str] = []
        agent_end_at: float | None = None
        settled = False

        def handle(event: dict) -> tuple[str, dict] | None:
            nonlocal agent_end_at, settled
            kind = event.get("type")
            if kind == "extension_ui_request" and event.get("method") in DIALOG_METHODS:
                return "question", event
            if kind == "turn_end":
                for part in (event.get("message") or {}).get("content", []):
                    if part.get("type") == "text":
                        text.append(part.get("text", ""))
            if kind == "agent_end":
                agent_end_at = time.time()
            if kind == "agent_settled":
                settled = True
            return None

        for event in list(buffered or []):
            hit = handle(event)
            if hit:
                return hit[0], hit[1], "".join(text)
        if settled:
            return "done", None, "".join(text)
        for event in self._events(deadline):
            if event is None:
                if agent_end_at is not None and time.time() - agent_end_at > 2.0:
                    return "done", None, "".join(text)
                continue
            hit = handle(event)
            if hit:
                return hit[0], hit[1], "".join(text)
            if settled:
                return "done", None, "".join(text)
        if settled or agent_end_at is not None:
            return "done", None, "".join(text)
        return "running", None, "".join(text)

    def abort(self, session_id: str) -> None:
        self.request({"type": "abort", "sessionId": session_id}, timeout=10)

    def close_session(self, session_id: str) -> bool:
        try:
            resp, _ = self.request({"type": "close_session", "sessionId": session_id},
                                   timeout=10)
            return bool((resp.get("data") or {}).get("closed", True))
        except HostError:
            return False
```

- [ ] **Step 4: Run the unit tests and verify they pass**

Run: `python3 -m unittest tests.test_host_client_unit -v`
Expected: 6 tests, `OK`.

- [ ] **Step 5: Re-run all existing tests**

Run: `python3 -m unittest discover -s tests -t . -v`
Expected: all tests `OK` (framing, questions, sessions, host-client unit).

- [ ] **Step 6: Commit**

```bash
git add host_client.py tests/test_host_client_unit.py
git commit -m "feat: add omo host rpc client"
```

---

### Task 5: Tool schemas, start/wait/status/cancel, registration

**Files:**
- Create: `schemas.py`
- Create: `tools.py`
- Modify: `__init__.py`
- Modify: `plugin.yaml`
- Create: `tests/test_tools_unit.py`

**Interfaces:**
- Consumes: `host_client`, `questions`, `sessions`.
- Produces: tool handlers `handle_start(args, **kwargs)`, `handle_wait(args, **kwargs)`, `handle_status(args, **kwargs)`, `handle_cancel(args, **kwargs)`; `tools.register(ctx)`.

- [ ] **Step 1: Write the failing helper tests**

`tests/test_tools_unit.py`:

```python
import os
import tempfile
import unittest
from pathlib import Path

from omo import tools


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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python3 -m unittest tests.test_tools_unit -v`
Expected: `ImportError` for `omo.tools`.

- [ ] **Step 3: Implement `schemas.py`**

```python
"""OpenAI-format tool schemas for the omo plugin."""

PERMISSIONS = ["full-access", "workspace", "accept-edits", "auto", "read-only", "ask"]

OMO_START = {
    "name": "omo_start",
    "description": (
        "Start a NEW OmO (oh-my-openagent) session. Every call creates a fresh session; "
        "use omo_resume to continue an older one. Returns immediately with a session_id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Prompt or slash command, e.g. /ulw-plan ..."},
            "cwd": {"type": "string", "description": "Absolute working directory"},
            "model": {"type": "string", "description": "Optional provider/id"},
            "permission": {"type": "string", "enum": PERMISSIONS},
            "name": {"type": "string", "description": "Optional OmO session name"},
            "parallel": {"type": "boolean", "default": False,
                         "description": "Allow a second session while one is running"},
        },
        "required": ["task"],
        "additionalProperties": False,
    },
}

OMO_WAIT = {
    "name": "omo_wait",
    "description": (
        "Wait for the next OmO event. Returns status=question with a question payload, "
        "status=done with the final text, or status=running on timeout."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": 120, "default": 90},
        },
        "additionalProperties": False,
    },
}

OMO_ANSWER = {
    "name": "omo_answer",
    "description": (
        "Answer the pending OmO question and wait for the next question or completion. "
        "Use the question ids and option labels from the omo_wait payload."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "request_id": {"type": "string"},
            "answers": {"type": "object"},
            "comment": {"type": "string"},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": 120, "default": 90},
        },
        "required": ["request_id", "answers"],
        "additionalProperties": False,
    },
}

OMO_STATUS = {
    "name": "omo_status",
    "description": "Show the status of an OmO session.",
    "parameters": {
        "type": "object",
        "properties": {"session_id": {"type": "string"}},
        "additionalProperties": False,
    },
}

OMO_CANCEL = {
    "name": "omo_cancel",
    "description": "Abort the running turn and close the OmO session.",
    "parameters": {
        "type": "object",
        "properties": {"session_id": {"type": "string"}},
        "additionalProperties": False,
    },
}

OMO_RESUME = {
    "name": "omo_resume",
    "description": (
        "Reopen a finished OmO session by its sessionPath. Pass message to start a new turn, "
        "or omit it to attach in idle state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "message": {"type": "string"},
        },
        "required": ["session_id"],
        "additionalProperties": False,
    },
}

ALL = [OMO_START, OMO_WAIT, OMO_ANSWER, OMO_STATUS, OMO_CANCEL, OMO_RESUME]
```

- [ ] **Step 4: Implement `tools.py` (start/wait/status/cancel)**

```python
"""Hermes tool handlers for the omo plugin."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import questions, schemas
from .host_client import (
    HostClient, HostError, OpenResult, TransportGone,
    check_protocol, default_socket_path, ensure_host, host_status,
)
from .sessions import SessionStore, new_session_id

_CTX = None
_STORE: SessionStore | None = None
_STORE_LOCK = threading.Lock()


def _cfg(key: str, default):
    if _CTX is None:
        return default
    try:
        return _CTX.get_config(key, default=default)
    except Exception:
        return default


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir("omo")
    except Exception:
        return Path.home() / ".hermes" / "plugin-data" / "omo"


def get_store() -> SessionStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = SessionStore(_data_dir() / "data.db")
        return _STORE


def _socket_path() -> str:
    return _cfg("host_socket", None) or default_socket_path()


def _json(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _err(code: str, detail: str = "", hint: str = "") -> str:
    return _json({"ok": False, "error": code, "detail": detail, "hint": hint})


def _split_model(model: str | None) -> tuple[str | None, str | None]:
    if not model:
        return None, None
    if "/" in model:
        provider, model_id = model.split("/", 1)
        return provider, model_id
    return None, model


def _validate_permission(permission: str) -> None:
    if permission not in schemas.PERMISSIONS:
        raise HostError("invalid_permission",
                        f"unknown permission preset: {permission}",
                        f"Use one of {schemas.PERMISSIONS}")


def _resolve_cwd(cwd: str | None) -> str:
    value = os.path.expanduser(cwd or _cfg("default_cwd", "/home/frappe/frappe-bench"))
    if not os.path.isabs(value):
        raise HostError("invalid_cwd", f"not an absolute path: {value}")
    real = os.path.realpath(value)
    if not os.path.isdir(real):
        raise HostError("invalid_cwd", f"not a directory: {value}")
    roots = _cfg("allowed_roots", []) or []
    if roots:
        allowed = [os.path.realpath(r) for r in roots]
        if not any(real == root or real.startswith(root + os.sep) for root in allowed):
            raise HostError("invalid_cwd", f"outside allowed_roots: {real}")
    return real


def _open_host() -> HostClient:
    host = HostClient(_socket_path())
    try:
        host.connect()
    except HostError:
        ensure_host(_socket_path())
        host = HostClient(_socket_path())
        host.connect()
    return host


def _attach(host: HostClient, rec) -> OpenResult:
    if not rec["session_path"]:
        raise HostError("session_lost", f"{rec['id']} has no session path")
    return host.open_session(session_path=rec["session_path"])


def _timeout(args: dict) -> int:
    return max(1, min(int(args.get("timeout_s") or _cfg("wait_timeout_s", 90)), 120))


def _next_state(host: HostClient, opened, timeout_s: int):
    frames = [e for e in opened.buffered if e.get("method") in
              {"question", "select", "input", "confirm"}]
    if not frames:
        frames = [e for e in (opened.state.get("pendingQuestions") or [])
                  if e.get("method") in {"question", "select", "input", "confirm"}]
    return host.collect(timeout_s, buffered=frames)


def _collect_with_retry(rec, timeout_s: int):
    """Attach, read, and retry once if the socket drops mid-wait."""
    last_exc = None
    for _ in range(2):
        host = _open_host()
        try:
            opened = _attach(host, rec)
            return _next_state(host, opened, timeout_s)
        except TransportGone as exc:
            last_exc = exc
        finally:
            host.close()
    raise last_exc


def _question_result(store: SessionStore, rec, frame: dict, text: str) -> str:
    payload = questions.normalize_frame(frame)
    if payload is None:
        raise HostError("omo_error", "unsupported question frame")
    store.update(rec["id"], status="question",
                 pending_request_id=payload["request_id"],
                 pending_question=json.dumps(payload, ensure_ascii=False),
                 last_text=text)
    expires_at = None
    if payload.get("timeout_s"):
        expires_at = (datetime.now(timezone.utc)
                      + timedelta(seconds=payload["timeout_s"])).isoformat()
    return _json({"ok": True, "session_id": rec["id"], "status": "question",
                  "question": payload, "text": text, "expires_at": expires_at})


def _done_result(store: SessionStore, rec, text: str) -> str:
    store.update(rec["id"], status="done", last_text=text,
                 pending_request_id=None, pending_question=None)
    if _cfg("auto_close", True):
        try:
            host = _open_host()
            try:
                opened = _attach(host, rec)
                host.close_session(opened.session_id)
            finally:
                host.close()
        except Exception:
            pass
    return _json({"ok": True, "session_id": rec["id"], "status": "done", "text": text})


def _resolve(store: SessionStore, hermes_session_id: str, session_id: str | None):
    rec = store.resolve(hermes_session_id, session_id)
    if rec is None:
        raise HostError("session_not_found", "no omo session for this conversation",
                        "Call omo_start first")
    return rec


def handle_start(args: dict, **kwargs) -> str:
    try:
        hermes_sid = kwargs.get("session_id") or ""
        store = get_store()
        running = store.active_for(hermes_sid)
        if running is not None and not args.get("parallel"):
            return _err("session_running", f"{running['id']} is still {running['status']}",
                        "Call omo_wait or omo_cancel, or pass parallel=true")
        cwd = _resolve_cwd(args.get("cwd"))
        permission = args.get("permission") or _cfg("permission_default", "full-access")
        _validate_permission(permission)
        provider, model_id = _split_model(args.get("model"))
        sid = new_session_id()
        ensure_host(_socket_path())
        with HostClient(_socket_path()) as host:
            check_protocol(host.protocol_info())
            opened = host.open_session(cwd=cwd,
                                       name=args.get("name") or f"hermes-{sid}",
                                       permission=permission,
                                       provider=provider, model_id=model_id)
            try:
                host.prompt(opened.session_id, args["task"])
            except HostError:
                host.close_session(opened.session_id)
                raise
            for _ in range(10):
                if opened.session_path:
                    break
                opened.session_path = host.get_state(opened.session_id).get("sessionFile")
                if not opened.session_path:
                    time.sleep(0.2)
        store.create(id=sid, hermes_session_id=hermes_sid,
                     omo_session_id=opened.session_id,
                     durable_session_id=opened.durable_session_id,
                     session_path=opened.session_path, cwd=cwd,
                     model=args.get("model"), permission=permission, status="running")
        return _json({"ok": True, "session_id": sid, "status": "running",
                      "cwd": cwd, "model": args.get("model"),
                      "permission": permission})
    except HostError as exc:
        return _err(exc.code, exc.detail, exc.hint)
    except Exception as exc:
        return _err("omo_error", str(exc))


def handle_wait(args: dict, **kwargs) -> str:
    try:
        store = get_store()
        rec = _resolve(store, kwargs.get("session_id") or "", args.get("session_id"))
        if rec["status"] in ("done", "cancelled", "error"):
            return _json({"ok": True, "session_id": rec["id"], "status": rec["status"],
                          "text": rec["last_text"] or ""})
        if rec["status"] == "idle":
            return _json({"ok": True, "session_id": rec["id"], "status": "idle",
                          "text": rec["last_text"] or ""})
        status, frame, text = _collect_with_retry(rec, _timeout(args))
        if status == "question":
            return _question_result(store, rec, frame, text)
        if status == "done":
            return _done_result(store, rec, text)
        store.update(rec["id"], status="running", last_text=text)
        return _json({"ok": True, "session_id": rec["id"], "status": "running", "text": text})
    except HostError as exc:
        return _err(exc.code, exc.detail, exc.hint)
    except Exception as exc:
        return _err("omo_error", str(exc))


def handle_status(args: dict, **kwargs) -> str:
    try:
        store = get_store()
        rec = _resolve(store, kwargs.get("session_id") or "", args.get("session_id"))
        info = {"reachable": False}
        try:
            status = host_status(_socket_path())
            info = {"reachable": bool(status.get("reachable")),
                    "pid": status.get("pid"), "generation": status.get("generation")}
        except Exception:
            pass
        return _json({
            "ok": True, "session_id": rec["id"], "status": rec["status"],
            "cwd": rec["cwd"], "model": rec["model"], "permission": rec["permission"],
            "created_at": rec["created_at"],
            "elapsed_s": int((datetime.now(timezone.utc)
                              - datetime.fromisoformat(rec["created_at"])).total_seconds()),
            "last_text": rec["last_text"] or "",
            "pending_question": json.loads(rec["pending_question"])
            if rec["pending_question"] else None,
            "host": info,
        })
    except HostError as exc:
        return _err(exc.code, exc.detail, exc.hint)
    except Exception as exc:
        return _err("omo_error", str(exc))


def handle_cancel(args: dict, **kwargs) -> str:
    try:
        store = get_store()
        rec = _resolve(store, kwargs.get("session_id") or "", args.get("session_id"))
        if rec["session_path"]:
            try:
                host = _open_host()
                try:
                    opened = _attach(host, rec)
                    try:
                        host.abort(opened.session_id)
                    except HostError:
                        pass
                    host.close_session(opened.session_id)
                finally:
                    host.close()
            except HostError:
                pass
        store.update(rec["id"], status="cancelled",
                     pending_request_id=None, pending_question=None)
        return _json({"ok": True, "session_id": rec["id"], "status": "cancelled"})
    except HostError as exc:
        return _err(exc.code, exc.detail, exc.hint)
    except Exception as exc:
        return _err("omo_error", str(exc))


def _check_requirements() -> bool:
    return shutil.which("omo") is not None


def register(ctx) -> None:
    global _CTX
    _CTX = ctx
    for schema, handler in (
        (schemas.OMO_START, handle_start),
        (schemas.OMO_WAIT, handle_wait),
        (schemas.OMO_STATUS, handle_status),
        (schemas.OMO_CANCEL, handle_cancel),
    ):
        ctx.register_tool(name=schema["name"], toolset="omo", schema=schema,
                          handler=handler, emoji="🤖", check_fn=_check_requirements)
```

- [ ] **Step 5: Update `__init__.py` and `plugin.yaml`**

`__init__.py`:

```python
"""Hermes-OmO RPC bridge plugin."""


def register(ctx):
    from . import tools
    tools.register(ctx)
```

`plugin.yaml`:

```yaml
name: omo
version: 0.1.0
description: "OmO (oh-my-openagent) RPC bridge: interactive omo sessions from Hermes."
author: Selçuk ÇALIŞKAN
requires_hermes: ">=0.21.5"
provides_tools:
  - omo_start
  - omo_wait
  - omo_status
  - omo_cancel
provides_hooks: []
```

- [ ] **Step 6: Run the unit tests**

Run: `python3 -m unittest tests.test_tools_unit -v`
Expected: 7 tests, `OK`.

- [ ] **Step 7: Run plugin doctor**

Run: `hermes plugins doctor ~/.hermes/plugins/omo`
Expected: no errors; registered tools match `provides_tools`.

- [ ] **Step 8: Commit**

```bash
git add schemas.py tools.py __init__.py plugin.yaml tests/test_tools_unit.py
git commit -m "feat: add omo start/wait/status/cancel tools"
```

---

### Task 6: Answer, resume, and `/omo` slash command

**Files:**
- Modify: `tools.py`
- Modify: `__init__.py`
- Modify: `plugin.yaml`
- Create: `tests/test_command_unit.py`

**Interfaces:**
- Consumes: Task 5 helpers and handlers.
- Produces: `handle_answer(args, **kwargs)`, `handle_resume(args, **kwargs)`, `handle_command(raw_args, **kwargs)`; registration of all six tools and `/omo`.

- [ ] **Step 1: Write the failing command test**

`tests/test_command_unit.py`:

```python
import unittest

from omo import tools


class TestCommandUsage(unittest.TestCase):
    def test_help(self):
        self.assertIn("/omo status", tools.handle_command("help"))

    def test_unknown_subcommand(self):
        self.assertIn("Usage:", tools.handle_command("nonsense"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m unittest tests.test_command_unit -v`
Expected: `AttributeError: module 'omo.tools' has no attribute 'handle_command'`.

- [ ] **Step 3: Add `handle_answer`, `handle_resume`, `handle_command` to `tools.py`**

Append:

```python
def handle_answer(args: dict, **kwargs) -> str:
    try:
        store = get_store()
        rec = _resolve(store, kwargs.get("session_id") or "", args.get("session_id"))
        if not rec["pending_question"]:
            return _err("omo_error", "no pending question for this session",
                        "Call omo_wait first")
        payload = json.loads(rec["pending_question"])
        if args.get("request_id") and args["request_id"] != payload["request_id"]:
            return _err("omo_error", "request_id does not match the pending question",
                        "Call omo_status to refresh")
        body = questions.build_answer(payload, args.get("answers") or {},
                                      args.get("comment") or "")
        host = _open_host()
        try:
            opened = _attach(host, rec)
            try:
                host.answer(opened.session_id, payload["request_id"], body)
            except HostError as exc:
                if exc.code != "question_already_resolved":
                    raise
            # The answered question is still in the attach snapshot; drop only it.
            opened.buffered = [e for e in opened.buffered
                               if e.get("id") != payload["request_id"]]
            opened.state["pendingQuestions"] = [
                e for e in (opened.state.get("pendingQuestions") or [])
                if e.get("id") != payload["request_id"]
            ]
            status, frame, text = _next_state(host, opened, _timeout(args))
        finally:
            host.close()
        if status == "question":
            return _question_result(store, rec, frame, text)
        if status == "done":
            return _done_result(store, rec, text)
        store.update(rec["id"], status="running", last_text=text,
                     pending_request_id=None, pending_question=None)
        return _json({"ok": True, "session_id": rec["id"], "status": "running", "text": text})
    except HostError as exc:
        return _err(exc.code, exc.detail, exc.hint)
    except Exception as exc:
        return _err("omo_error", str(exc))


def handle_resume(args: dict, **kwargs) -> str:
    try:
        store = get_store()
        session_id = args.get("session_id")
        if not session_id:
            return _err("omo_error", "session_id is required", "Use /omo list")
        rec = store.get(session_id)
        if rec is None:
            return _err("session_not_found", f"unknown session: {session_id}",
                        "Use /omo list")
        if not rec["session_path"]:
            return _err("session_lost", f"{session_id} has no session path")
        if rec["status"] in ("running", "question") and args.get("message"):
            return _err("session_running", f"{session_id} is still {rec['status']}",
                        "Call omo_wait or omo_cancel first")
        message = args.get("message")
        host = _open_host()
        try:
            opened = host.open_session(session_path=rec["session_path"])
            if message:
                host.prompt(opened.session_id, message)
        finally:
            host.close()
        status = "running" if message else "idle"
        store.update(rec["id"], status=status, omo_session_id=opened.session_id,
                     durable_session_id=opened.durable_session_id or rec["durable_session_id"],
                     session_path=opened.session_path or rec["session_path"],
                     pending_request_id=None, pending_question=None)
        return _json({"ok": True, "session_id": rec["id"], "status": status,
                      "text": rec["last_text"] or ""})
    except HostError as exc:
        return _err(exc.code, exc.detail, exc.hint)
    except Exception as exc:
        return _err("omo_error", str(exc))


USAGE = "Usage: /omo status | /omo list | /omo stop | /omo resume <id> [message]"


def _format_status(data: dict) -> str:
    lines = [f"{data['session_id']}  {data['status']}  {data.get('cwd', '')}"]
    if data.get("last_text"):
        lines.append(data["last_text"][-600:])
    if data.get("pending_question"):
        q = data["pending_question"]["questions"][0]
        lines.append(f"Pending: {q['header']} — {q['question']}")
    return "\n".join(lines)


def handle_command(raw_args: str, **kwargs) -> str:
    parts = (raw_args or "").strip().split()
    sub = parts[0] if parts else "status"
    if sub in ("help", "-h", ""):
        return USAGE
    try:
        hermes_sid = kwargs.get("session_id") or ""
        if sub == "status":
            data = json.loads(handle_status({}, session_id=hermes_sid))
            return _format_status(data) if data.get("ok") else f"Error: {data.get('detail')}"
        if sub == "list":
            rows = get_store().list_recent(hermes_sid, limit=10)
            if not rows:
                return "No omo sessions."
            return "\n".join(f"{r['id']}  {r['status']}  {r['cwd']}" for r in rows)
        if sub == "stop":
            data = json.loads(handle_cancel({}, session_id=hermes_sid))
            return f"{data.get('session_id')}: {data.get('status')}" if data.get("ok") \
                else f"Error: {data.get('detail')}"
        if sub == "resume":
            if len(parts) < 2:
                return "Usage: /omo resume <id> [message]"
            payload = {"session_id": parts[1]}
            if len(parts) > 2:
                payload["message"] = " ".join(parts[2:])
            data = json.loads(handle_resume(payload, session_id=hermes_sid))
            return f"{data.get('session_id')}: {data.get('status')}" if data.get("ok") \
                else f"Error: {data.get('detail')}"
        return USAGE
    except Exception as exc:
        return f"omo: {exc}"
```

- [ ] **Step 4: Register the new tools and command**

Update the `register` function in `tools.py`:

```python
def register(ctx) -> None:
    global _CTX
    _CTX = ctx
    for schema, handler in (
        (schemas.OMO_START, handle_start),
        (schemas.OMO_WAIT, handle_wait),
        (schemas.OMO_ANSWER, handle_answer),
        (schemas.OMO_STATUS, handle_status),
        (schemas.OMO_CANCEL, handle_cancel),
        (schemas.OMO_RESUME, handle_resume),
    ):
        ctx.register_tool(name=schema["name"], toolset="omo", schema=schema,
                          handler=handler, emoji="🤖", check_fn=_check_requirements)
    ctx.register_command("omo", handle_command, description="Control OmO sessions")
    skill = Path(__file__).parent / "skills" / "omo-orchestration" / "SKILL.md"
    if skill.exists():
        ctx.register_skill("omo-orchestration", skill)
```

Update `plugin.yaml`:

```yaml
provides_tools:
  - omo_start
  - omo_wait
  - omo_answer
  - omo_status
  - omo_cancel
  - omo_resume
```

- [ ] **Step 5: Run the command tests and the full unit suite**

Run: `python3 -m unittest tests.test_command_unit -v`
Expected: 2 tests, `OK`.

Run: `python3 -m unittest discover -s tests -t . -v`
Expected: all unit tests `OK`.

- [ ] **Step 6: Run plugin doctor**

Run: `hermes plugins doctor ~/.hermes/plugins/omo`
Expected: all six tools and the `/omo` command registered; no errors.

- [ ] **Step 7: Commit**

```bash
git add tools.py __init__.py plugin.yaml tests/test_command_unit.py
git commit -m "feat: add omo answer/resume tools and /omo command"
```

---

### Task 7: Bundled skill and documentation

**Files:**
- Create: `skills/omo-orchestration/SKILL.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: all tools from Tasks 5–6.
- Produces: `ctx.register_skill("omo-orchestration", path)` registration (already coded in Task 6).

- [ ] **Step 1: Write the skill**

`skills/omo-orchestration/SKILL.md`:

```markdown
---
name: omo-orchestration
description: "Use when delegating an interactive task to OmO (oh-my-openagent): /ulw-plan, /ulw-execute, ultrawork, or any omo session that may ask questions. Covers start, wait, clarify, answer, and resume."
---

# OmO orchestration

OmO runs as an external agent behind its own host daemon. You drive it with six tools.

## The loop

1. `omo_start(task="...", cwd="...")` — always a NEW session.
2. `omo_wait(session_id)` — repeat until `status` is `question` or `done`.
3. On `question`: ask the user with `clarify`, using the exact option labels.
4. `omo_answer(session_id, request_id, answers)` — sends the answer and waits
   for the next question or completion.
5. Repeat 3–4. On `done`, report `text` to the user.

Never call `omo_wait` on an `idle` session; it returns immediately.

## Answer shape

```json
{"q1": {"selected": ["Approve"], "text": ""}}
```

- `selected` holds one or more exact option labels.
- `text` carries a free-text answer (for `freeText` questions or a comment).
- For `kind=select` / `input` / `confirm`, the plugin maps the answer back to
  the native OmO value automatically. Still pass the question id from the payload.

## clarify mapping

- One `clarify` call for all questions in the payload (max 5).
- `multiSelect: true` → `clarify` `multi_select: true`.
- `options[].label` must be passed verbatim.
- `freeText: true` → no choices; use the user's text.
- If `clarify` returns no answer, send `{"comment": "no answer"}` only when the
  question is optional; for approval gates, ask again.

## /ulw-plan recipe

```
omo_start(task="/ulw-plan <request>", cwd="/home/frappe/frappe-bench")
omo_wait(session_id)
# question: intent / approval / change approach
clarify(...)
omo_answer(session_id, request_id, answers)
# repeat until done; verify the plan file path in the final text
```

## New task vs resume

- New task → `omo_start` (fresh session, no prior context).
- Continue an old session → `omo_resume(session_id, message)`.
- Do not use `omo_resume` to start a new task.

## Errors

- `session_running` → `omo_wait` or `omo_cancel`, or pass `parallel=true`.
- `host_unavailable` → the plugin tries `omo host ensure` once; if it persists,
  report the error.
- `session_lost` → start a new session.
- `protocol_mismatch` → the installed OmO version is incompatible; stop.

## Security

The default permission preset is `full-access`: OmO can run any command and write
anywhere with the Hermes user's privileges. Only use it for trusted work; pass
`permission="workspace"` or `"accept-edits"` when the task should be narrower.
```

- [ ] **Step 2: Update `README.md`**

Append:

```markdown
## Usage

```
omo_start(task="/ulw-plan add a health endpoint", cwd="/home/frappe/frappe-bench")
omo_wait(session_id)
# → question payload; answer with clarify + omo_answer
```

Bundled skill: `omo:omo-orchestration`.
```

- [ ] **Step 3: Validate the plugin**

Run: `hermes plugins doctor ~/.hermes/plugins/omo`
Expected: no errors; the skill is registered.

Run: `hermes plugins validate ~/.hermes/plugins/omo`
Expected: no errors. If validation requires catalog fields, fix only the reported
manifest fields; do not remove `provides_tools`.

- [ ] **Step 4: Commit**

```bash
git add skills/omo-orchestration/SKILL.md README.md
git commit -m "docs: add omo orchestration skill"
```

---

### Task 8: Host integration tests and `/ulw-plan` end-to-end acceptance

**Files:**
- Create: `tests/test_host_integration.py`
- Create: `docs/acceptance/2026-10-19-ulw-plan.md`

**Interfaces:**
- Consumes: `HostClient`, `ensure_host`, `check_protocol`, `normalize_frame`, `build_answer`, `SessionStore`.
- Produces: verified evidence that the plugin's transport and Q&A loop work.

- [ ] **Step 1: Write the opt-in integration tests**

`tests/test_host_integration.py`:

```python
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
                host.prompt(opened.session_id, PROMPT)
                status, frame, _ = host.collect(90, buffered=opened.buffered)
                self.assertEqual(status, "question")
                _answer_first_option(host, opened, frame)
                status, _, text = host.collect(90)
                self.assertEqual(status, "done")
                self.assertIn("ANSWER=", text)

    def test_pending_question_replays_after_disconnect(self):
        ensure_host()
        with tempfile.TemporaryDirectory() as cwd:
            host = HostClient()
            host.connect()
            check_protocol(host.protocol_info())
            opened = host.open_session(cwd=cwd, permission="full-access",
                                       name="hermes-replay")
            host.prompt(opened.session_id, PROMPT)
            status, frame, _ = host.collect(90, buffered=opened.buffered)
            self.assertEqual(status, "question")
            path = opened.session_path
            host.close()
            time.sleep(1.0)
            with HostClient() as host2:
                opened2 = host2.open_session(session_path=path)
                pending = [e for e in (opened2.state.get("pendingQuestions") or [])
                           if e.get("method") == "question"]
                self.assertTrue(pending, "pending question was not replayed")
                _answer_first_option(host2, opened2, pending[0])
                status, _, text = host2.collect(90)
                self.assertEqual(status, "done")
                self.assertIn("ANSWER=", text)

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
                self.assertEqual(resumed.session_path, path)
                host2.prompt(resumed.session_id, "Reply with exactly: RESUME_OK")
                status, _, text = host2.collect(90)
                self.assertEqual(status, "done")
                self.assertIn("RESUME_OK", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the integration tests against the real host**

Run: `OMO_INTEGRATION=1 python3 -m unittest tests.test_host_integration -v`
Expected: 3 tests, `OK`. Each test makes real OmO/LLM calls (~30–90 s each).

- [ ] **Step 3: Run the full suite**

Run: `python3 -m unittest discover -s tests -t . -v`
Expected: all unit tests `OK`; integration tests skipped without `OMO_INTEGRATION=1`.

- [ ] **Step 4: Run the `/ulw-plan` end-to-end acceptance**

Create a scratch repo:

```bash
mkdir -p ~/.hermes/cache/scratch/omo-e2e && cd ~/.hermes/cache/scratch/omo-e2e
git init -q
```

Start the session from Hermes (through the plugin tools, not the CLI):

```text
omo_start(task="/ulw-plan create a single-file python script hello.py that prints hello", cwd="~/.hermes/cache/scratch/omo-e2e")
```

Then loop:

```text
omo_wait(session_id)
# → status=question (intent or approval)
clarify(...)
omo_answer(session_id, request_id, answers)
# repeat until status=done
```

Pass criteria:
- At least one `question` payload reaches the user through `clarify`.
- The answer is accepted; the next `omo_wait`/`omo_answer` continues the same session.
- `status=done` and the final text names a plan file under `.omo/drafts/` or `.omo/plans/`.
- The plan file exists in the scratch repo.
- `omo_status` shows `done`; `/omo list` shows the session.
- A subsequent `omo_start` creates a different `session_id` and does not carry the
  previous conversation.

- [ ] **Step 5: Record the acceptance evidence**

`docs/acceptance/2026-10-19-ulw-plan.md`:

```markdown
# /ulw-plan acceptance — 2026-10-19

- Plugin commit:
- OmO version: 5.1.35
- Host socket:
- Session id:
- Session path:
- Questions asked (headers):
- Answers:
- Plan file:
- Final status:
- New-session check:

## Commands run

```text
omo_start(...)
omo_wait(...)
clarify(...)
omo_answer(...)
```

## Result

PASS / FAIL with a one-line reason.
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_host_integration.py docs/acceptance/2026-10-19-ulw-plan.md
git commit -m "test: add host integration and ulw-plan acceptance"
```

---

### Task 9: Hardening pass and release tag

**Files:**
- Modify: any file needed by findings.
- Modify: `README.md`.
- Modify: `plugin.yaml` (version bump only if behavior changed).

**Interfaces:**
- Consumes: everything above.
- Produces: a clean, documented v0.1.0.

- [ ] **Step 1: Run the complete suite and doctor**

Run: `python3 -m unittest discover -s tests -t . -v`
Expected: all `OK`.

Run: `hermes plugins doctor ~/.hermes/plugins/omo`
Expected: no errors.

Run: `hermes plugins validate ~/.hermes/plugins/omo`
Expected: no errors.

- [ ] **Step 2: Check that plugin import does not slow Hermes startup**

Run:

```bash
python3 -c "import time,sys; sys.path.insert(0,'/home/frappe/.hermes/plugins'); t=time.time(); import omo; print(f'{time.time()-t:.3f}s')"
```

Expected: under 0.05 s (the package only defines `register`).

- [ ] **Step 3: Review error paths manually**

- Stop the host (`omo host stop --json`), then call `omo_start`.
  Expected: the plugin runs `omo host ensure` once and starts the session.
- Call `omo_start` twice without `parallel`.
  Expected: `session_running`.
- Call `omo_start` with `cwd="/nope"`.
  Expected: `invalid_cwd`.
- Call `omo_answer` with a stale `request_id`.
  Expected: `omo_error` with the refresh hint.

- [ ] **Step 4: Final commit and tag**

```bash
git add -A
git commit -m "chore: hardening and v0.1.0"
git tag v0.1.0
```

- [ ] **Step 5: Report**

Report: plugin path, commit hash, test counts, integration result, `/ulw-plan`
acceptance result, and any remaining risks.
```

---

## Self-Review

**Spec coverage:**
- D1 `/ulw-plan` e2e → Task 8.
- D2 host + attach → Tasks 4, 8.
- D3 `full-access` default → Task 5 config default.
- D4 `cwd` default → Task 5 `_resolve_cwd`.
- D5 new session per start → Task 5 `handle_start`.
- D6 auto-close → Task 5 `_done_result`.
- D7 resume explicit → Task 6 `handle_resume`.
- D8 running guard / parallel → Task 5 `handle_start`.
- D9 stateless plugin → Tasks 4–6 (each call opens a socket).
- D10 local first → entire plan.
- Spec §5 protocol gotchas → Task 4 `parse_open_response`, `HostClient.answer`, `collect(buffered=...)`.
- Spec §6 SQLite → Task 3.
- Spec §7 tools → Tasks 5–6.
- Spec §8 normalization → Task 2.
- Spec §9 lifecycle → Tasks 5–6.
- Spec §10 error codes → Tasks 5–6.
- Spec §11 security → Task 5 `_resolve_cwd`/`_validate_permission`, Task 7 skill warning.
- Spec §12 tests → Tasks 1–4, 6, 8, 9.
- Spec §13 out of scope → not implemented.
- Spec §14 risks → Task 9 error paths, Task 8 replay.

**Placeholder scan:** no `TBD`, `TODO`, or “implement later” steps; every code step
contains the code.

**Type consistency:** `OpenResult.session_path`, `HostClient.collect` returning
`(status, frame, text)`, `SessionStore.resolve(hermes_session_id, session_id)`,
and `questions.build_answer(payload, answers, comment)` are used consistently in
Tasks 4–8.
```

