# G4 — Evidence, Result Package & Observability

Status: **in progress**. G4.1 is accepted in production on 2026-09-17 at schema `20260917_0010`; G4.2 is developed on a separate feature branch. G4 does not grant Git write, merge, deploy, secret or external-write authority.

## Objective

Make execution progress and completion evidence durable, correlated and inspectable. The system reports observed state only: stage/role, elapsed time, heartbeat, tool activity, runner checks, retries/errors and actual recorded token/cost usage. It never invents a completion percentage.

## G4.1 — Durable evidence foundation

- append-only/idempotent `execution_evidence` stream correlated by `execution_id`;
- durable OpenCode message/tool observations without persisting raw tool inputs/outputs;
- `usage_events.execution_id` correlation for execution-level token/cost accounting;
- content-addressed `execution_result_packages` with provisional/final state;
- read-only evidence/result-package APIs;
- progress API enriched with current role, actual tool calls, runner checks, retry count and recorded cost;
- every terminal execution materializes a result package: provisional only while a workspace inspection is genuinely pending, otherwise final immediately;
- terminal result package is immutable after finalization;
- schema revision `20260917_0010`.

## Result package v1

The package records the original task, source base/head identity, workspace change digest/count, execution result, runner check evidence, structured review/artifact evidence when available, actual recorded cost and explicit limitations. The digest is SHA-256 over canonical JSON.

G4.1 intentionally does not claim full changed-file names/diff, structured child-run QA/reviewer verdicts, artifact provenance, or automatic provider token/cost extraction. Missing evidence is reported as a limitation rather than fabricated.

## G4.2 — Child-run telemetry and structured review

- durable `execution_child_runs` records real OpenCode child sessions with parent task-call identity, role, provider/model, observed status and timestamps;
- retry lineage is linked only after an observed terminal failure with the same role + task fingerprint; no retry is invented when evidence is ambiguous;
- automatic OpenCode session token/cost snapshots are correlated into `usage_events` by idempotent source identity; manual `/api/usage` cannot impersonate these internal sources;
- QA/reviewer prompts emit a bounded `AI_ORCHESTRA_VERDICT` contract; only validated verdict fields are persisted, never raw task tool input/output;
- Result Package v2 includes child runs, retry lineage, structured verdicts and automatic-usage provenance;
- read-only child-run API is available at `/api/executions/{execution_id}/child-runs`;
- schema revision `20260917_0011`.

G4.2 still reports full changed-file names/diff and generated artifact provenance as explicit limitations. Those claims are not fabricated.

## Next increments

G4.3 adds operator-facing timelines, metrics/alerts, changed-file/artifact provenance completion and stronger provenance requirements for `general-high-assurance` profiles.

## Acceptance gates

G4.1 required migration/restore compatibility, schema-shape checks, idempotent evidence capture, tool-payload redaction, terminal-path package coverage, immutable final-package tests, progress recovery when OpenCode is unavailable, full unit/API suite, Docker isolation/recovery/backup-restore/PostgreSQL CI, review/merge, then a separate migration-first production rollout and smoke acceptance.

G4.2 additionally requires exact child-session/task-call correlation tests, idempotent automatic usage snapshots, redacted structured verdict tests, conservative retry-lineage tests, `0010 -> 0011` migration/restore coverage, full suite and the same review/merge/production change-control separation.
