---
name: omo-orchestration
description: "Use when delegating an interactive task to OmO (oh-my-openagent): /ulw-plan, /ulw-execute, ultrawork, or any omo session that may ask questions. Covers start, wait, clarify, answer, and resume."
---

# OmO orchestration

OmO runs as an external agent behind its own host daemon. You drive it with six tools.

## The loop

1. `omo_start(task="...", cwd="...")` — always a NEW session.
2. `omo_wait(session_id)` — repeat until `status` is `question` or a terminal state:
   `done`, `cancelled`, or `error` (surfaced as `omo_error`). Stop there; do not loop.
3. On `question`: ask the user with `clarify`, using the exact option labels.
4. `omo_answer(session_id, request_id, answers)` — sends the answer and waits
   for the next question or completion.
5. Repeat 3–4. On `done`, report `text` to the user.
   `done` is only reported after OmO's `agent_settled`; `agent_end` alone is a
   turn yield and may be followed by retries or a consultant continuation.

Never call `omo_wait` on an `idle` session; it returns immediately. To continue
one, call `omo_resume(session_id, message)`; for a new task, call `omo_start`.

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
- If `clarify` returns no answer, call
  `omo_answer(session_id, request_id, answers={}, comment="no answer")` only when
  the question is optional; for approval gates, ask again.

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
- Close the current session and optionally start fresh → `/omo_new [--cwd PATH]
  [--permission PRESET] [task]`. With no task it only closes; the next
  `omo_start` starts clean.
- Continue an old session → `omo_resume(session_id, message)`.
- Do not use `omo_resume` to start a new task.

## Errors

- `session_running` → `omo_wait` or `omo_cancel`, or pass `parallel=true` to
  `omo_start` only (`omo_resume` has no `parallel`).
- `host_unavailable` → the plugin tries `omo host ensure` once; if it persists,
  report the error.
- `session_lost` → call `omo_cancel(session_id)` and then `omo_start`, or call
  `omo_start(..., parallel=true)`.
- `protocol_mismatch` → the installed OmO version is incompatible; stop.

## Security

The default permission preset is `full-access`: OmO can run any command and write
anywhere with the Hermes user's privileges. Only use it for trusted work; pass
`permission="workspace"` or `"accept-edits"` when the task should be narrower.
