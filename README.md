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

## Status and limitations

- Version 0.1.0: Hermes plugin exposing six tools (`omo_start`, `omo_wait`,
  `omo_answer`, `omo_status`, `omo_cancel`, `omo_resume`), the `/omo` command
  (`status|list|stop|resume`), and the bundled skill `omo:omo-orchestration`.
- Requires OmO 5.1.35 (protocolVersion 1) and a reachable host socket
  (`~/.omo/agent/rpc/rpc.sock` by default). `omo host ensure` runs on demand;
  a cold start that misses the CLI's own readiness window is retried once.
- Default permission preset is `full-access`; the security warning and the
  restricted-preset guidance live in the bundled skill.
- Known limitations:
  - A `done` result can be a turn yield while an OmO consultant lane keeps
    running. `auto_close` is on by default, so the session is closed at that
    point and the promised continuation cannot be collected.
  - `/omo list` is verified at the handler level (`tools.handle_command`); the
    slash-command surface itself has not been exercised.
  - Command fallback scoping is single-user: with no Hermes session id the
    command targets the most recent stored session.

Tests:

```bash
python3 -m unittest discover -s tests -t . -v   # 90 tests, 3 skipped
OMO_INTEGRATION=1 python3 -m unittest tests.test_host_integration -v
```

The three host integration tests talk to the real OmO runtime and are opt-in
through `OMO_INTEGRATION=1`.
