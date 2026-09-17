# G4 — Evidence, Result Package & Observability

Status: **in progress**. G4 starts after the accepted G3 production baseline and its pre-G4 hardening rollout. G4 does not grant Git write, merge, deploy, secret or external-write authority.

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

## Next increments

G4.2 persists child-run role/model/status/timestamps, retry lineage, structured reviewer/QA verdicts and automatic provider usage/cost capture. G4.3 adds operator-facing timelines, metrics/alerts and stronger provenance requirements for `general-high-assurance` profiles.

## Acceptance gates

G4.1 requires migration/restore compatibility, schema-shape checks, idempotent evidence capture, tool-payload redaction, terminal-path package coverage, immutable final-package tests, progress recovery when OpenCode is unavailable, full unit/API suite, Docker isolation/recovery/backup-restore/PostgreSQL CI, review/merge, then a separate migration-first production rollout and smoke acceptance.
