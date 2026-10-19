"""OmO host RPC client. Hermes-agnostic: socket, JSONL framing, protocol."""

from __future__ import annotations

import json
import os
import select
import shutil
import socket
import subprocess
import time
from collections import deque
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


def _host_cmd(args: list[str], timeout: int,
              ok_codes: tuple[int, ...] = (0,)) -> dict:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HostError("host_unavailable", f"timed out after {timeout}s",
                        "Run: omo host ensure") from exc
    if proc.returncode not in ok_codes:
        raise HostError("host_unavailable", proc.stderr.strip() or proc.stdout.strip(),
                        "Run: omo host ensure")
    try:
        return _last_json_line(proc.stdout)
    except ValueError as exc:
        raise HostError("host_unavailable", f"invalid host output: {exc}",
                        "Run: omo host ensure") from exc


def host_status(socket_path: str | None = None, timeout: int = 15) -> dict:
    omo = shutil.which("omo")
    if not omo:
        raise HostError("host_unavailable", "omo binary not found", "Install omo")
    args = [omo, "host", "status", "--json"]
    if socket_path:
        args += ["--socket", socket_path]
    # `omo host status` exits 3 while the host is down, yet still prints the
    # status JSON; a bare non-zero code must not hide reachable=false.
    data = _host_cmd(args, timeout, ok_codes=(0, 3))
    if "reachable" not in data:
        # Exit 3 with unrelated JSON on stdout is not a status report.
        raise HostError("host_unavailable", f"unexpected host status output: {data}",
                        "Run: omo host ensure")
    return data


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
    # `omo host ensure` gives a cold host 10 s to answer get_protocol_info and
    # exits non-zero when it misses that window, though the daemon often comes
    # up right after. Retry once and probe first, so a start immediately after
    # `omo host stop` does not fail with host_unavailable.
    deadline = time.monotonic() + timeout
    last: HostError | None = None
    for _ in range(2):
        try:
            return _host_cmd(args, max(1, int(deadline - time.monotonic())))
        except HostError as exc:
            last = exc
            time.sleep(1)
            try:
                status = host_status(socket_path, timeout=5)
            except HostError:
                continue
            if status.get("reachable"):
                return status
    assert last is not None
    raise last


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
        self._pending: deque[dict] = deque()
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
        self.attach_socket(sock)

    def attach_socket(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = JsonlBuffer()
        self._pending.clear()
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
        while True:
            while self._pending:
                yield self._pending.popleft()
            if time.time() >= deadline:
                return
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
            self._pending.extend(self._buf.feed(chunk))

    def request(self, payload: dict, timeout: float = 15.0,
                keep_extra: bool = True) -> tuple[dict, list[dict]]:
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
                if keep_extra and extra:
                    self._pending.extendleft(reversed(extra))
                if not event.get("success"):
                    err = event.get("error") or ""
                    raise HostError(event.get("errorCode") or err or "omo_error", err, "")
                return event, extra
            extra.append(event)
        if keep_extra and extra:
            self._pending.extendleft(reversed(extra))
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
        resp, extra = self.request(payload, timeout=20, keep_extra=False)
        opened = parse_open_response(resp, fallback_path=session_path)
        opened.buffered = [e for e in extra if e.get("type") == "extension_ui_request"]
        return opened

    def prompt(self, session_id: str, message: str) -> str:
        # A cold host may spend >20s booting the agent runtime before it acks.
        resp, _ = self.request({"type": "prompt", "sessionId": session_id,
                                "message": message}, timeout=60)
        return (resp.get("data") or {}).get("disposition", "")

    def get_state(self, session_id: str) -> dict:
        resp, _ = self.request({"type": "get_state", "sessionId": session_id}, timeout=10)
        return (resp.get("data") or {}).get("state") or {}

    def answer(self, session_id: str, ui_request_id: str, body: dict) -> None:
        payload = dict(body or {})
        payload.update({"type": "extension_ui_response", "sessionId": session_id,
                        "uiRequestId": ui_request_id})
        self.request(payload, timeout=15)

    def collect(self, timeout_s: float = 90, buffered: list[dict] | None = None
                ) -> tuple[str, dict | None, str]:
        deadline = time.time() + max(1.0, min(float(timeout_s), 120.0))
        text: list[str] = []
        settled = False

        def handle(event: dict) -> tuple[str, dict] | None:
            nonlocal settled
            kind = event.get("type")
            if kind == "extension_ui_request" and event.get("method") in DIALOG_METHODS:
                return "question", event
            if kind == "turn_end":
                for part in (event.get("message") or {}).get("content", []):
                    if part.get("type") == "text":
                        text.append(part.get("text", ""))
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
                continue
            hit = handle(event)
            if hit:
                return hit[0], hit[1], "".join(text)
            if settled:
                return "done", None, "".join(text)
        # agent_end alone is only one low-level run; retries, compaction and
        # consultant continuations can still follow. Only agent_settled is
        # terminal, so an unfinished turn stays running and the session open.
        if settled:
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
