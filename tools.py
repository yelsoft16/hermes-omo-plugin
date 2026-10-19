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
