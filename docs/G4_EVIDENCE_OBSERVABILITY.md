# G4 — Evidence, Result Package & Observability

Status: **G4.3 engineering complete and deployed to production on 2026-09-23** at merge `b5f6179` with schema `20260917_0011`. G4.1-G4.3 engineering, CI and production smoke gates are complete. One external acceptance limitation remains: a fresh provider-backed full end-to-end QA run has not been re-confirmed because provider balance is unavailable; no paid provider call is required to establish the engineering state below. G4 does not grant Git write, merge, deploy, secret or external-write authority.

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

G4.2 originally left full changed-file names/diff and generated artifact provenance as explicit limitations. G4.3 closes the trusted changed-file and generated-artifact provenance gaps while intentionally continuing not to persist a full source diff.

## G4.3 — Operator timeline, observability & provenance completeness

- read-only `/api/executions/{execution_id}/timeline` merges execution lifecycle, durable evidence, child-run boundaries and runner checks into one chronological operator view;
- read-only `/api/observability/summary` reports durable execution/runner/workspace counts plus evidence-backed alerts for deadline/lease/retry/cleanup/cost conditions;
- trusted workspace inspection persists normalized changed-file names and artifact provenance for each changed path: kind, SHA-256 and size for regular files/symlinks, or explicit deleted state;
- Result Package v2 consumes generated-artifact provenance only from trusted workspace inspection and requires the provenance path-set to match trusted changed files; malformed or mismatched provenance is not promoted into the final package;
- `general-high-assurance` and `regulated-critical` Result Packages expose a machine-checkable assurance block covering immutable source identity, trusted changed-file identity, trusted artifact digests, runner snapshot/checkpoint/image binding when changes exist, and automatic provider-usage provenance;
- incomplete high-assurance final packages surface a critical `high_assurance_provenance_incomplete` observability alert; standard-tier repositories do not acquire this mandatory baseline;
- terminal Result Package ordering guarantees trusted workspace inspection/audit evidence is flushed before final package materialization;
- no schema migration was required beyond `20260917_0011`, and no new Git/deploy/external-write authority was introduced.

G4.3 engineering acceptance is supported by full CI, post-merge CI, backup verification and read-only production smoke. The only outstanding acceptance item is a fresh provider-backed full end-to-end QA confirmation once provider balance is available.

## Acceptance gates

G4.1 required migration/restore compatibility, schema-shape checks, idempotent evidence capture, tool-payload redaction, terminal-path package coverage, immutable final-package tests, progress recovery when OpenCode is unavailable, full unit/API suite, Docker isolation/recovery/backup-restore/PostgreSQL CI, review/merge, then a separate migration-first production rollout and smoke acceptance.

G4.2 additionally requires exact child-session/task-call correlation tests, idempotent automatic usage snapshots, redacted structured verdict tests, conservative retry-lineage tests, `0010 -> 0011` migration/restore coverage, full suite and the same review/merge/production change-control separation.

G4.3 additionally passed changed-file/artifact provenance regression coverage, symlink/deletion/race-safe inspection hardening, operator timeline and observability API tests, high-assurance completeness/alert tests, Docker buildability, disposable-runner isolation, durable Runner Manager, Temporal PoC, backup/restore disaster recovery and PostgreSQL migration semantics. Production rollout used a verified backup and read-only health/schema/observability smoke. Provider-backed full E2E remains deliberately outside this engineering gate until balance is available.
