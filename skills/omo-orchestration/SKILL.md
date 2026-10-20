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
5. Repeat 3–4. On `done`, report `text` to the user. The session is NOT closed:
   it becomes `idle` and stays available.

Continuing vs starting new:

- Same session, follow-up → `omo_resume(session_id, message)`.
- New session → `omo_start` (fresh) or `/omo_new` (closes the current one and
  starts fresh). Closing is always an explicit choice.
- `omo_wait` on an `idle` session returns `idle` immediately; it does not block.
- `auto_close=true` is opt-in and restores the old "close on settle" behavior.

State semantics: `running` means OmO is working; `question` means OmO asked and
is waiting for your answer (the plugin re-presents it on the next `omo_wait` or
`omo_status`, without contacting the host); `idle` means the turn settled and
the session is ready for the next instruction, not closed.

If a clarify question times out, do NOT cancel or close the session. Leave it
open and, on the user's next message, call `omo_status`: it reconciles a missed
`agent_settled` by reading the transcript (last assistant stopReason `stop`, no
open tool call, stop-state count 0) and reports `idle` **only when no answer is
pending**. Re-present the pending question, then continue with `omo_resume`.
`omo_cancel` is only for explicit abandonment.

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

## Permission presets in headless RPC sessions

OmO asks for approval before shell/eval commands in every preset except
`full-access`. An RPC session has no attached user, so the prompt can never be
answered and the turn waits forever. Consequences:

- `read-only`, `workspace`, `accept-edits`, and `auto` are only safe for tasks
  that need no shell command at all. A "read-only investigation" that must run
  `bench`/`python`/`grep` WILL hang under `read-only`.
- Use `full-access` for any task that executes commands, and enforce the
  read-only intent in the task text ("only SELECT/read, do not write"). Do not
  rely on the preset to express it.
- Say explicitly in `task` that hindsight_* tools must not be called; they can
  block on this host.

## Is it hung, or waiting?

Before cancelling, collect evidence instead of guessing:

1. `omo host status --json` - `sessions.worker` counts sub-agent lanes. `worker: 0`
   means OmO did not delegate; there is no child agent to wait for.
2. Session transcript (`omo_status` -> `session_path`): check `mtime` and the last
   record. A last record that is `toolCall` with no matching `toolResult` means a
   tool is blocked - approval deadlock or a stuck tool - not a sub-agent.
3. Process tree: `pgrep -aP <session-runtime-pid>`; no bash/python children while
   a shell tool is pending confirms the tool never started.
4. Silence is normal while a model turn runs. A long gap alone is not proof of a
   hang; a pending tool call for many minutes is.

## Security

The default permission preset is `full-access`: OmO can run any command and write
anywhere with the Hermes user's privileges. Only use it for trusted work; pass
`permission="workspace"` or `"accept-edits"` when the task should be narrower and
does not need shell commands (see the headless deadlock above).
