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
            "workflow": {
                "type": "string",
                "enum": ["ulw-plan", "ulw-execute", "mass-ulw"],
                "description": (
                    "Optional explicit OmO workflow. Prefixes a bare task with the "
                    "slash command. Required for workflows whose review gate is "
                    "session-scoped (plan-reviewer), because resuming an old session "
                    "cannot reopen it."
                ),
            },
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
