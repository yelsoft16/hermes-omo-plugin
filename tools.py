"""Hermes tool handlers for the omo plugin."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import questions, schemas
from .host_client import (
    DIALOG_METHODS, HostClient, HostError, OpenResult, TransportGone,
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
        if not any(_under(real, root) for root in allowed):
            raise HostError("invalid_cwd", f"outside allowed_roots: {real}")
    return real


def _under(path: str, root: str) -> bool:
    """True when path is root or inside it; commonpath handles root="/"."""
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _open_host() -> HostClient:
    # Resume/wait/answer may run after the on-demand host daemon has gone
    # away. Probe (and start) it first with a cold-start budget; the connect
    # retry stays as a race guard for a daemon that dies between the probe
    # and the socket connect.
    ensure_host(_socket_path(), timeout=90)
    host = HostClient(_socket_path(), connect_timeout=10)
    try:
        host.connect()
    except HostError:
        ensure_host(_socket_path(), timeout=90)
        host = HostClient(_socket_path(), connect_timeout=10)
        host.connect()
    return host


def _safe_close(session_id: str) -> None:
    """Best-effort close of a host session; never raises."""
    try:
        host = _open_host()
        try:
            host.close_session(session_id)
        finally:
            host.close()
    except Exception:
        pass


def _attach(host: HostClient, rec) -> OpenResult:
    if not rec["session_path"]:
        raise HostError("session_lost", f"{rec['id']} has no session path")
    return host.open_session(session_path=rec["session_path"])


def _timeout(args: dict) -> int:
    value = args.get("timeout_s")
    if value is None:
        value = _cfg("wait_timeout_s", 90)
    return max(1, min(int(value), 120))


def _next_state(host: HostClient, opened, timeout_s: float):
    frames = [e for e in opened.buffered if e.get("method") in DIALOG_METHODS]
    if not frames:
        frames = [e for e in (opened.state.get("pendingQuestions") or [])
                  if e.get("method") in DIALOG_METHODS]
    return host.collect(timeout_s, buffered=frames)


def _collect_with_retry(rec, timeout_s: int):
    """Attach, read, and retry once if the socket drops mid-wait.

    One deadline covers every attempt so a retry cannot double the wait.
    """
    deadline = time.time() + timeout_s
    last_exc: TransportGone | None = None
    for _ in range(2):
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        host = _open_host()
        try:
            opened = _attach(host, rec)
            return _next_state(host, opened, remaining)
        except TransportGone as exc:
            last_exc = exc
        finally:
            host.close()
    if last_exc is not None and time.time() < deadline:
        raise last_exc
    return "running", None, ""


def _matches_request(event: dict, request_id: str | None) -> bool:
    """True when a UI event is the question identified by ``request_id``.

    The wire shape is unconfirmed: the spike used ``uiRequestId`` while
    ``questions.normalize_frame`` stores ``id``, so accept either.
    """
    return bool(request_id) and request_id in (event.get("id"), event.get("uiRequestId"))


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
    # The settled turn is complete, but the OmO session stays open by default
    # so a follow-up can continue in the same session (matching the manual
    # workflow: work until you close it or start a new one). Store it as
    # `idle` -- `done` is reserved for the auto_close path, where the host
    # session really is closed. The tool still answers `status="done"` for
    # this turn so callers stop waiting.
    auto_close = bool(_cfg("auto_close", False))
    store.update(rec["id"], status="done" if auto_close else "idle", last_text=text,
                 pending_request_id=None, pending_question=None)
    if auto_close:
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
        task = args.get("task")
        if not isinstance(task, str) or not task.strip():
            raise HostError("omo_error", "task is required")
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
        opened = None
        ensure_host(_socket_path())
        with HostClient(_socket_path()) as host:
            check_protocol(host.protocol_info())
            opened = host.open_session(cwd=cwd,
                                       name=args.get("name") or f"hermes-{sid}",
                                       permission=permission,
                                       provider=provider, model_id=model_id)
            try:
                host.prompt(opened.session_id, task)
                for _ in range(10):
                    if opened.session_path:
                        break
                    opened.session_path = host.get_state(
                        opened.session_id).get("sessionFile")
                    if not opened.session_path:
                        time.sleep(0.2)
                if not opened.session_path:
                    raise HostError("session_lost",
                                    "host did not return a session path")
                store.create(id=sid, hermes_session_id=hermes_sid,
                             omo_session_id=opened.session_id,
                             durable_session_id=opened.durable_session_id,
                             session_path=opened.session_path, cwd=cwd,
                             model=args.get("model"), permission=permission,
                             status="running", title=_clip_title(task))
            except Exception:
                _safe_close(opened.session_id)
                raise
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
        if rec["status"] == "error":
            return _err("omo_error", rec["last_text"] or "")
        if rec["status"] in ("done", "cancelled"):
            return _json({"ok": True, "session_id": rec["id"], "status": rec["status"],
                          "text": rec["last_text"] or ""})
        if rec["status"] == "idle":
            return _json({"ok": True, "session_id": rec["id"], "status": "idle",
                          "text": rec["last_text"] or ""})
        if rec["status"] == "question" and rec["pending_question"]:
            # The host is still waiting for this answer; re-present it instead
            # of collecting again, so a late user reply continues the same turn.
            return _json({"ok": True, "session_id": rec["id"], "status": "question",
                          "question": json.loads(rec["pending_question"]),
                          "text": rec["last_text"] or ""})
        status, frame, text = _collect_with_retry(rec, _timeout(args))
        if status == "question":
            if frame is None:
                raise HostError("omo_error", "question event missing")
            return _question_result(store, rec, frame, text)
        if status == "done":
            return _done_result(store, rec, text)
        settled = None if rec["pending_request_id"] else \
            _transcript_settled_text(rec["session_path"])
        if settled is not None:
            return _done_result(store, rec, settled)
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
        if rec["status"] == "running" and not rec["pending_request_id"]:
            settled = _transcript_settled_text(rec["session_path"])
            if settled is not None:
                store.update(rec["id"], status="idle", last_text=settled,
                             pending_request_id=None, pending_question=None)
                row = store.get(rec["id"])
                if row is not None:
                    rec = row
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
            "session_path": rec["session_path"],
            "OMO_SESSION_ID": rec["omo_session_id"],
            "durable_session_id": rec["durable_session_id"],
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
        if rec["status"] in ("done", "cancelled", "error"):
            return _json({"ok": True, "session_id": rec["id"], "status": rec["status"]})
        if rec["session_path"]:
            try:
                host = _open_host()
                try:
                    opened = _attach(host, rec)
                    try:
                        host.abort(opened.session_id)
                    except Exception:
                        pass
                    try:
                        host.close_session(opened.session_id)
                    except Exception:
                        pass
                finally:
                    host.close()
            except Exception:
                pass
        store.update(rec["id"], status="cancelled",
                     pending_request_id=None, pending_question=None)
        return _json({"ok": True, "session_id": rec["id"], "status": "cancelled"})
    except HostError as exc:
        return _err(exc.code, exc.detail, exc.hint)
    except Exception as exc:
        return _err("omo_error", str(exc))


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
            # Clear the pending fields before collecting so a transport drop cannot
            # leave status naming a question that was already answered.
            store.update(rec["id"], status="running",
                         pending_request_id=None, pending_question=None)
            opened.buffered = [e for e in opened.buffered
                               if not _matches_request(e, payload["request_id"])]
            opened.state["pendingQuestions"] = [
                e for e in (opened.state.get("pendingQuestions") or [])
                if not _matches_request(e, payload["request_id"])
            ]
            status, frame, text = _next_state(host, opened, _timeout(args))
        finally:
            host.close()
        if status == "question":
            if frame is None:
                raise HostError("omo_error", "question event missing")
            return _question_result(store, rec, frame, text)
        if status == "done":
            return _done_result(store, rec, text)
        store.update(rec["id"], status="running", last_text=text)
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
        message = args.get("message")
        if rec["status"] in ("running", "question"):
            if message:
                return _err("session_running", f"{session_id} is still {rec['status']}",
                            "Call omo_wait or omo_cancel first")
            # A live session keeps its pending question. Reopening it here would
            # write idle and hide the question from omo_wait, so report the stored
            # state as-is instead of attaching.
            pending = json.loads(rec["pending_question"]) if rec["pending_question"] else None
            status = "question" if rec["status"] == "question" and pending else "running"
            return _json({"ok": True, "session_id": rec["id"], "status": status,
                          "question": pending, "text": rec["last_text"] or ""})
        host = _open_host()
        try:
            opened = host.open_session(session_path=rec["session_path"])
            if message:
                try:
                    host.prompt(opened.session_id, message)
                except Exception:
                    _safe_close(opened.session_id)
                    raise
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


USAGE = ("Usage: /omo status | /omo list | /omo stop | /omo new [--cwd PATH] "
         "[--permission PRESET] [task] | /omo resume <number|id> [message]")


def _format_status(data: dict) -> str:
    lines = [f"{data['session_id']}  {data['status']}  {data.get('cwd', '')}"]
    if data.get("last_text"):
        lines.append(data["last_text"][-600:])
    pending = data.get("pending_question")
    questions = pending.get("questions") if isinstance(pending, dict) else None
    if questions and isinstance(questions[0], dict):
        q = questions[0]
        lines.append(f"Pending: {q.get('header', '')} — {q.get('question', '')}")
    return "\n".join(lines)


TITLE_LIMIT = 80


def _clip_title(text: str, limit: int = TITLE_LIMIT) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 3].rstrip() + "..."


def _title_from_transcript(path: str | None, limit: int = TITLE_LIMIT) -> str:
    """First user prompt in an OmO transcript, used for sessions stored before titles."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(500), fh):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "message":
                    continue
                msg = obj.get("message") or {}
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    if content.strip():
                        return _clip_title(content, limit)
                    continue
                for part in content or []:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text") or ""
                        if text.strip():
                            return _clip_title(text, limit)
    except OSError:
        return ""
    return ""


def _transcript_settled_text(path: str | None) -> str | None:
    """Settled fallback for a missed `agent_settled` event.

    A turn is settled when the last assistant message stopped normally, carries
    no open tool call, and the stop-state hook count is zero. Returns the final
    text, or None while the transcript still shows work in flight.
    """
    if not path:
        return None
    last = None
    stop_count = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                typ = obj.get("type")
                if typ == "message":
                    msg = obj.get("message") or {}
                    if msg.get("role") != "assistant":
                        continue
                    parts = msg.get("content") or []
                    has_tool = any(
                        isinstance(p, dict) and p.get("type") in
                        ("toolCall", "tool_use", "tool-call") for p in parts)
                    text = "".join(
                        p.get("text") or "" for p in parts
                        if isinstance(p, dict) and p.get("type") == "text")
                    last = (msg.get("stopReason"), has_tool, text)
                    stop_count = None
                elif typ == "custom" and obj.get("customType") == "senpi.hooks.stop-state":
                    stop_count = (obj.get("data") or {}).get("count")
    except OSError:
        return None
    if last and last[0] == "stop" and not last[1] and stop_count == 0:
        return last[2]
    return None


def _session_title(rec) -> str:
    title = (rec["title"] or "").strip()
    if title:
        return title
    title = _title_from_transcript(rec["session_path"])
    if title:
        return title
    return _clip_title(rec["last_text"] or "") or "untitled"


def _format_list_row(index: int, rec) -> str:
    created = rec["created_at"] or ""
    try:
        when = datetime.fromisoformat(created).astimezone().strftime("%d.%m %H:%M")
    except ValueError:
        when = created[:16]
    return (f"{index}. {_session_title(rec)}\n"
            f"   {rec['status']}  ·  {when}  ·  {rec['id']}")


def _command_hermes_sid(**kwargs) -> str:
    """Best-effort Hermes session id for a slash-command handler.

    Command handlers receive only ``raw_args``; the gateway binds the
    ``HERMES_SESSION_*`` context vars without a session id (so the ContextVar is
    an explicit ``""``), hence the ``os.environ`` last resort.
    """
    value = kwargs.get("session_id") or ""
    if value:
        return value
    try:
        from gateway.session_context import get_session_env
        value = get_session_env("HERMES_SESSION_ID", "") or ""
    except Exception:
        value = ""
    return value or os.environ.get("HERMES_SESSION_ID", "") or ""


def _command_target(**kwargs):
    """The session a bare ``/omo`` command refers to.

    Commands run before an agent turn exists, so with no id they fall back to the
    most recent stored session globally (single-user local plugin). An id that
    does not match a stored row is ignored for the same reason.
    """
    store = get_store()
    hermes_sid = _command_hermes_sid(**kwargs)
    return (store.resolve(hermes_sid, None) if hermes_sid else None) or store.latest()


def handle_new(raw_args: str = "", **kwargs) -> str:
    """Close the current OmO session and optionally start a fresh one."""
    try:
        tokens = shlex.split(raw_args or "")
        cwd = None
        permission = None
        task_parts: list[str] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token in ("--cwd", "-C") and index + 1 < len(tokens):
                cwd = tokens[index + 1]
                index += 2
            elif token == "--permission" and index + 1 < len(tokens):
                permission = tokens[index + 1]
                index += 2
            else:
                task_parts.append(token)
                index += 1
        task = " ".join(task_parts).strip()
        hermes_sid = _command_hermes_sid(**kwargs)
        store = get_store()
        rec = store.active_for(hermes_sid) if hermes_sid else None
        if rec is None:
            rec = store.latest()
        closed = None
        if rec is not None and rec["status"] in ("starting", "running", "question"):
            data = json.loads(handle_cancel({"session_id": rec["id"]}))
            if data.get("ok"):
                closed = rec["id"]
        if not task:
            return (f"Closed {closed}. Ready for a new session." if closed
                    else "No active session. Ready for a new session.")
        payload = {"task": task}
        if cwd:
            payload["cwd"] = cwd
        if permission:
            payload["permission"] = permission
        data = json.loads(handle_start(payload, session_id=hermes_sid))
        if not data.get("ok"):
            return f"Error: {data.get('detail') or data.get('error')}"
        return f"Started {data.get('session_id')} ({data.get('status', 'running')})"
    except Exception as exc:
        return f"omo_new: {exc}"


def _command_rows(store, hermes_sid: str) -> list:
    """The same rows `/omo list` shows, so numbers match what the user sees."""
    return (store.list_recent(hermes_sid, limit=10) if hermes_sid else []) \
        or store.list_all(limit=10)


def handle_command(raw_args: str, **kwargs) -> str:
    raw = (raw_args or "").strip()
    parts = raw.split()
    sub = parts[0] if parts else "status"
    if sub in ("help", "-h"):
        return USAGE
    try:
        if sub == "status":
            rec = _command_target(**kwargs)
            if rec is None:
                return "No omo sessions."
            data = json.loads(handle_status({"session_id": rec["id"]}))
            return _format_status(data) if data.get("ok") else f"Error: {data.get('detail')}"
        if sub == "list":
            store = get_store()
            hermes_sid = _command_hermes_sid(**kwargs)
            rows = _command_rows(store, hermes_sid)
            if not rows:
                return "No omo sessions."
            return f"OmO sessions ({len(rows)}):\n" + "\n".join(
                _format_list_row(i, r) for i, r in enumerate(rows, 1))
        if sub == "stop":
            rec = _command_target(**kwargs)
            if rec is None:
                return "No omo sessions."
            data = json.loads(handle_cancel({"session_id": rec["id"]}))
            return f"{data.get('session_id')}: {data.get('status')}" if data.get("ok") \
                else f"Error: {data.get('detail')}"
        if sub == "new":
            rest = raw.split(None, 1)[1] if len(parts) > 1 else ""
            return handle_new(rest, **kwargs)
        if sub == "resume":
            if len(parts) < 2:
                return "Usage: /omo resume <number|id> [message]"
            ref = parts[1]
            if ref.isdigit():
                rows = _command_rows(get_store(), _command_hermes_sid(**kwargs))
                index = int(ref)
                if not 1 <= index <= len(rows):
                    return f"No session number {ref}; /omo list shows 1..{len(rows)}."
                ref = rows[index - 1]["id"]
            payload = {"session_id": ref}
            if len(parts) > 2:
                payload["message"] = " ".join(parts[2:])
            data = json.loads(handle_resume(payload))
            return f"{data.get('session_id')}: {data.get('status')}" if data.get("ok") \
                else f"Error: {data.get('detail')}"
        return USAGE
    except Exception as exc:
        return f"omo: {exc}"


def _check_requirements() -> bool:
    return shutil.which("omo") is not None


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
    ctx.register_command("omo", handle_command, description="Control OmO sessions",
                         args_hint="<status|list|stop|new|resume>")
    ctx.register_command("omo_new", handle_new,
                         description="Close the current OmO session and optionally start a new one",
                         args_hint="[--cwd PATH] [--permission PRESET] [task]")
    skill = Path(__file__).parent / "skills" / "omo-orchestration" / "SKILL.md"
    if skill.exists():
        ctx.register_skill("omo-orchestration", skill)
