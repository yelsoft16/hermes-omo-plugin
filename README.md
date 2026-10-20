# omo — Hermes plugin

Local Hermes plugin that drives OmO (oh-my-openagent) sessions over its RPC host.

- Start a new session: `omo_start`
- Wait for a question or completion: `omo_wait`
- Answer a question: `omo_answer`
- Control: `omo_status`, `omo_cancel`, `omo_resume`
- Slash command: `/omo status|list|stop|new|resume <n|id>`

Design: `docs/specs/2026-10-19-omo-plugin-design.md`

## Install

```bash
# Hermes Agent >= 0.21.5 and OmO 5.1.35 (oh-my-openagent) must be installed
hermes plugins install yelsoft16/hermes-omo-plugin --enable
```

The plugin talks to OmO's RPC host (`~/.omo/agent/rpc/rpc.sock`) and starts it
on demand with `omo host ensure`. Restart the Hermes gateway after installing
or updating so the new module is loaded.

`main` is the latest stable release, so a plain `hermes plugins install` always
clones it. Development happens on the `dev` branch; `-dev.N` tags are
checkpoints, not releases.

## Usage

```
omo_start(task="/ulw-plan add a health endpoint", cwd="/home/frappe/frappe-bench")
omo_wait(session_id)
# → question payload; answer with clarify + omo_answer
```

Bundled skill: `omo:omo-orchestration`.

## Status and limitations

- Version 0.2.1-dev.5: Hermes plugin exposing six tools (`omo_start`, `omo_wait`,
  `omo_answer`, `omo_status`, `omo_cancel`, `omo_resume`), the `/omo` command
  (`status|list|stop|new|resume`), the standalone `/omo_new` command, and the
  bundled skill `omo:omo-orchestration`.
- Requires OmO 5.1.35 (protocolVersion 1) and a reachable host socket
  (`~/.omo/agent/rpc/rpc.sock` by default). `omo host ensure` runs on demand;
  a cold start that misses the CLI's own readiness window is retried once.
- Default permission preset is `full-access`; the security warning and the
  restricted-preset guidance live in the bundled skill.
- Sessions stay open after a settled turn. `omo_wait` reports `status=done` for
  that turn, but the stored session becomes `idle` and the host session is not
  closed, matching the manual workflow: continue with
  `omo_resume(session_id, message)`; start a new one with `omo_start` or
  `/omo_new`. Set `auto_close=true` in the plugin config to close on settle
  instead (the old behavior).
- Known limitations:
  - `done` is reported only after `agent_settled`, so retries, compaction, and
    consultant continuations are not cut off. If OmO never settles, `omo_wait`
    keeps returning `running`; use `/omo stop` (or `/omo_new`) to abandon it.
  - `agent_settled` is not persisted/replayed by the host. If no client is
    attached when it fires (host restart or a dropped subscription), the
    session can stay `running` even though the turn finished.
  - `/omo list` is verified at the handler level (`tools.handle_command`); the
    slash-command surface itself has not been exercised.
  - Command fallback scoping is single-user: with no Hermes session id the
    command targets the most recent stored session.

Tests:

```bash
python3 -m unittest discover -s tests -t . -v   # 98 tests, 3 skipped
OMO_INTEGRATION=1 python3 -m unittest tests.test_host_integration -v
```

The three host integration tests talk to the real OmO runtime and are opt-in
through `OMO_INTEGRATION=1`.
