# OmO task prompt template

Use this for every non-trivial OmO task, especially `mass-ulw`. The prompt must
let an agent with no conversation history act without guessing.

## Template

````text
mass-ulw

## Goal
<one observable outcome>

## Context
- Repo/app/branch:
- Site/environment:
- Current behavior:
- Desired behavior:
- Key files, DocTypes, APIs:
- Concrete identifiers (supplier, item, price list, user, ID):

## Scope
- In scope:
- Out of scope (only if the user stated it):
- Allowed writes (only if the user stated it):

## Acceptance criteria
1. <observable result>
2. <observable result>
3. Tests: `<exact command>` passes.

## Verification
- Run: `<exact commands>`
- Report: files changed, commands + exit codes, skipped tests, remaining risks.

## Constraints
- <only constraints the user explicitly stated>
````

## Rules

1. Use `workflow="mass-ulw"` in `omo_start`, or start the task with the bare
   token `mass-ulw`. Do not use both; the plugin avoids a double prefix.
2. Write for an agent that knows nothing about the chat. Spell out paths,
   identifiers, and current vs desired behavior.
3. Define done as observable outcomes, not "make a plan". A plan is a means,
   not the deliverable.
4. Let mass-ulw choose the DAG. Do not prescribe agent counts or lanes. If
   parallel work is wanted, say "parallelize independent parts" as a positive
   instruction.
5. Never add meta-negations such as `PLAN YOK`, `plan yazma`, `review yok`,
   `agent kullanma`, `dosya yazma yok`, or `kod yazma yok` unless the user
   explicitly asked for them. They disable the skill's planning, review, or
   fan-out and produce a worse result.
6. Real guardrails belong in `## Constraints`: no live DB writes, no
   migrate/build/restart, no core changes. Keep them specific and factual.
7. Include domain invariants (for example "one currency per supplier" or "no
   1.0 fallback") when they matter.
8. If the request is small, say "use a single agent if that is faster"; do not
   force parallelism.

## Anti-patterns

- `PLAN YOK, dogrudan uygula` -> disables mass-ulw's DAG planning and fan-out.
- `SADECE REVIEW, uygulamaya gecme` on a fresh session -> makes the
  orchestrator stop at the first checkpoint.
- `dosya yazma YOK` -> kills the plan artifact and review gate.
- A wall of bullets with no goal or acceptance criteria -> the agent cannot
  tell when it is done.
