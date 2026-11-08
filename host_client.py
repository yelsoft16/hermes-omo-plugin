"""OmO host RPC client. Hermes-agnostic: socket, JSONL framing, protocol."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import select
import shutil
import socket
import stat
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


# OmO 5.1.62+ cold start dies in the supervisor bootstrap before the host can
# answer get_protocol_info. The child closes fd 0, fails to reopen /dev/fd/0,
# then writes that warning to a duplicated stderr fd that was already closed
# and reused as an eventfd; 82 bytes to an eventfd is EINVAL, and the unhandled
# write error kills the host. The host CLI itself is healthy.
_BOOTSTRAP_CRASH_MARKERS = ("before answering get_protocol_info", "EINVAL")


def _looks_like_bootstrap_crash(exc: HostError) -> bool:
    detail = f"{exc.detail} {exc}"
    return all(marker in detail for marker in _BOOTSTRAP_CRASH_MARKERS)


def _direct_host_log() -> str:
    directory = os.path.expanduser("~/.omo/agent/logs")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, "hermes-direct-host.log")


def _start_host_directly(socket_path: str | None = None,
                         timeout: float = 90) -> dict:
    """Run the host child the supervisor would have run, without the supervisor.

    Used only when the supported `omo host ensure` path is provably broken at
    bootstrap. The host registers its own endpoint, so `omo host status` sees
    it as reachable. Without a supervisor the empty-host exit applies; the
    plugin cold-starts it again when needed.
    """
    omo = shutil.which("omo")
    if not omo:
        raise HostError("host_unavailable", "omo binary not found", "Install omo")
    sock = socket_path or default_socket_path()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("SENPI_RPC_HOST_WATCH")
           and key not in {"SENPI_RPC_INTERNAL_KEY_STDIN",
                           "SENPI_RPC_HOST_GENERATION"}}
    with open(_direct_host_log(), "ab") as log:
        subprocess.Popen(
            [omo, "--mode", "rpc", "--multi-session", "--listen", f"unix://{sock}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
            start_new_session=True, env=env, cwd=os.path.expanduser("~"))
    deadline = time.monotonic() + max(45.0, float(timeout))
    last_error = ""
    while time.monotonic() < deadline:
        try:
            status = host_status(sock, timeout=5)
        except HostError as exc:
            last_error = exc.detail or str(exc)
            status = {}
        if status.get("reachable"):
            return status
        time.sleep(1)
    raise HostError(
        "host_unavailable",
        f"direct host did not become reachable at {sock}"
        + (f": {last_error}" if last_error else ""),
        "Run: omo host ensure")


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
            if _looks_like_bootstrap_crash(exc):
                try:
                    return _start_host_directly(socket_path,
                                                timeout=max(45, timeout))
                except HostError as direct_exc:
                    raise HostError(
                        "host_unavailable",
                        f"{exc.detail}; direct launch failed: {direct_exc.detail}",
                        "Run: omo host ensure") from direct_exc
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


_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_LAUNCH_ID = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$")


def _is_private_dir(path: str, exact_mode: int | None = None) -> bool:
    """A client-owned, non-symlink directory with no group/other write bits."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        return False
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        return False
    return exact_mode is None or stat.S_IMODE(st.st_mode) == exact_mode


def read_endpoint_record(agent_dir: str, launch_id: str) -> dict:
    """Read and validate `<agentDir>/rpc/keys/<launch_id>` (OmO socket_auth)."""
    if not _LAUNCH_ID.match(launch_id or ""):
        raise HostError("host_identity_unproven", f"malformed launch id: {launch_id!r}")
    rpc_dir = os.path.join(agent_dir, "rpc")
    keys_dir = os.path.join(rpc_dir, "keys")
    if not (_is_private_dir(agent_dir) and _is_private_dir(rpc_dir)
            and _is_private_dir(keys_dir, exact_mode=0o700)):
        raise HostError("unsafe_key_record",
                        f"unsafe endpoint key directory under {agent_dir}")
    path = os.path.join(keys_dir, launch_id)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise HostError("unsafe_key_record",
                        f"endpoint key record unreadable: {exc}") from exc
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise HostError("unsafe_key_record",
                            "endpoint key record is not a 0600 user file")
        raw = os.read(fd, 65536)
    finally:
        os.close(fd)
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HostError("unsafe_key_record",
                        "endpoint key record is not JSON") from exc
    if (not isinstance(record, dict) or record.get("version") != 1
            or record.get("launch_id") != launch_id
            or not isinstance(record.get("key"), str)
            or not _HEX64.match(record["key"])):
        raise HostError("unsafe_key_record",
                        "endpoint key record has the wrong shape")
    return record


class HostClient:
    def __init__(self, socket_path: str | None = None, connect_timeout: float = 5.0,
                 key_root: str | None = None) -> None:
        self.socket_path = socket_path or default_socket_path()
        self._connect_timeout = connect_timeout
        self.key_root = key_root or os.path.expanduser("~/.omo/agent")
        self._sock: socket.socket | None = None
        self._buf = JsonlBuffer()
        self._pending: deque[dict] = deque()
        self._seq = 0
        self._protocol: dict | None = None

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
        try:
            self.attach_socket(sock, handshake=False)
            self._handshake()
        except Exception:
            self.close()
            raise

    def attach_socket(self, sock: socket.socket, handshake: bool = True) -> None:
        self._sock = sock
        self._buf = JsonlBuffer()
        self._pending.clear()
        self._protocol = None
        if handshake:
            self._send({"id": "ci", "type": "set_client_info", "width": 100,
                        "capabilities": ["question"]})

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self._protocol = None

    def _handshake(self) -> None:
        """Probe the host and prove the per-launch endpoint key when challenged.

        OmO 5.1.6x advertises `socket_auth` and returns a challenge on the
        first protocol probe; older hosts ignore the extra `auth` field and
        answer normally, so one first frame works for both.
        """
        client_nonce = secrets.token_hex(32)
        resp, _ = self.request({"type": "get_protocol_info",
                                "auth": {"client_nonce": client_nonce}},
                               timeout=10)
        data = resp.get("data") or {}
        challenge = data.get("auth")
        if isinstance(challenge, dict):
            self._prove_endpoint_key(data, challenge, client_nonce)
        self._protocol = data
        self._send({"id": "ci", "type": "set_client_info", "width": 100,
                    "capabilities": ["question"]})

    def _prove_endpoint_key(self, data: dict, challenge: dict,
                            client_nonce: str) -> None:
        launch_id = challenge.get("launch_id")
        server_nonce = challenge.get("server_nonce")
        server_proof = challenge.get("server_proof")
        if not (isinstance(launch_id, str) and _LAUNCH_ID.match(launch_id)
                and isinstance(server_nonce, str) and _HEX64.match(server_nonce)
                and isinstance(server_proof, str) and _HEX64.match(server_proof)):
            raise HostError("host_identity_unproven",
                            "malformed endpoint authentication challenge")
        record = read_endpoint_record(data.get("agentDir") or self.key_root, launch_id)
        key = bytes.fromhex(record["key"])
        expected = hmac.new(
            key,
            "\n".join(("senpi-rpc-server", launch_id, client_nonce,
                       server_nonce)).encode("utf-8"),
            hashlib.sha256).digest()
        if not hmac.compare_digest(expected, bytes.fromhex(server_proof)):
            raise HostError("host_identity_unproven",
                            f"server proof mismatch for launch {launch_id}")
        client_proof = hmac.new(
            key,
            "\n".join(("senpi-rpc-client", launch_id, server_nonce,
                       client_nonce)).encode("utf-8"),
            hashlib.sha256).hexdigest()
        self.request({"type": "authenticate", "client_proof": client_proof},
                     timeout=10)

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
        if self._protocol is not None:
            return self._protocol
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
