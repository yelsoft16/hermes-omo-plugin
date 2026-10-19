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
