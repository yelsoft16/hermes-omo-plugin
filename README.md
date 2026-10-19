# omo — Hermes plugin

Local Hermes plugin that drives OmO (oh-my-openagent) sessions over its RPC host.

- Start a new session: `omo_start`
- Wait for a question or completion: `omo_wait`
- Answer a question: `omo_answer`
- Control: `omo_status`, `omo_cancel`, `omo_resume`
- Slash command: `/omo status|list|stop|resume <id>`

Design: `docs/specs/2026-10-19-omo-plugin-design.md`

## Usage

```
omo_start(task="/ulw-plan add a health endpoint", cwd="/home/frappe/frappe-bench")
omo_wait(session_id)
# → question payload; answer with clarify + omo_answer
```

Bundled skill: `omo:omo-orchestration`.
