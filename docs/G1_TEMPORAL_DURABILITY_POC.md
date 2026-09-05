# G1 Temporal durable-workflow PoC

## Status

This is an **isolated G1 proof of concept**, not a production deployment and not a replacement for Execution V1.

The PoC exists to answer one narrow question with executable evidence: can AI Orchestra move long-running orchestration state out of the browser/request lifecycle and recover deterministically after worker and workflow-engine process loss?

## Versions under test

As of 2026-09-05:

- Temporal Server PoC image: `temporalio/auto-setup:1.29.7`;
- Temporal Python SDK intent: `>=1.32,<1.33`, resolved by the repository's deterministic dev lock;
- disposable persistence backend: `postgres:16-alpine`.

`1.29.7` is the latest stable tag currently published for the deprecated `temporalio/auto-setup` image line. The server release number must not be assumed to map one-to-one to an `auto-setup` image tag. `auto-setup` is used here only for an isolated CI proof of concept; it is not the intended production server topology.

The Temporal server image is deliberately **not** added to the production `docker-compose.yml` in this slice.

## What the smoke test proves

`scripts/temporal-durable-smoke.sh` creates a disposable Docker network and disposable PostgreSQL + Temporal containers. Temporal is exposed only on an ephemeral loopback port.

The Python exercise then:

1. starts a workflow and worker #1;
2. starts a heartbeating activity;
3. forcibly kills worker #1 with SIGKILL while attempt 1 is running and requires a non-zero worker exit;
4. starts a distinct worker #2 on the same task queue;
5. requires Temporal to detect the lost heartbeat and retry the activity;
6. requires the workflow to complete only on attempt 2 or later;
7. records a canonical SHA-256 of the workflow result;
8. restarts the Temporal server container while preserving the disposable PostgreSQL database;
9. reconnects and reads the completed workflow result again;
10. fails if the post-restart result payload or digest differs.

This gives executable evidence for both:

- worker-process loss and activity retry;
- Temporal server process restart with workflow history/result persisted in PostgreSQL.

All disposable resources are removed after the test.

## What this PoC does **not** prove

Temporal does not provide exactly-once external effects by itself. This PoC therefore does **not** authorize or implement:

- production deploys;
- Git pushes or pull-request creation by agents;
- writes to external business systems;
- financial execution;
- access to secrets;
- exactly-once side effects;
- approval semantics;
- repository/worktree allocation;
- multi-agent fan-out;
- production Temporal HA, TLS, authentication or authorization;
- production backup/restore for Temporal persistence;
- production image/SBOM/vulnerability approval.

The existing fail-closed capability guard remains authoritative.

## Security and supply-chain boundary

`auto-setup` is deprecated and suitable only for this development/PoC boundary, not the target production topology. The server image is version-tagged here but is not yet digest-pinned or admitted through an AI Orchestra SBOM/vulnerability gate.

Before Temporal can enter the production trust boundary, the implementation must define and verify at least:

- a supported production Temporal server deployment topology rather than `auto-setup`;
- immutable image digests and provenance;
- vulnerability/SBOM admission policy;
- TLS and authenticated client/worker access;
- namespace and retention policy;
- PostgreSQL ownership, backup and recovery model;
- monitoring, alerting and capacity limits;
- worker versioning and safe workflow-code evolution;
- deterministic replay tests for deployed workflow histories.

## Required architecture before replacing Execution V1

The next durable-core work must sit above Temporal and remain application-owned:

1. canonical workflow/effect inputs with SHA-256 identity;
2. stable idempotency keys;
3. monotonic fencing generation for effect ownership;
4. approvals bound to exact immutable input/evidence digests;
5. transactional effect intent before external calls;
6. a minimal trusted effect executor outside model/agent authority;
7. result identity/digest verification;
8. commit/admission record after the effect;
9. reconciliation after crash, timeout or ambiguous response;
10. compensation where a reversible external action exists.

Temporal is the durable scheduler/history engine. It must not become policy authority, signing authority, approval authority or release authority.

## Promotion criteria

This PoC is eligible to inform the production design only if CI repeatedly demonstrates:

- attempt 1 starts on worker #1;
- worker #1 exits non-zero after forced loss;
- a distinct worker #2 is started;
- the same workflow completes via attempt >= 2 on worker #2;
- a Temporal server restart does not alter the persisted workflow result;
- no production Compose service or volume is touched by the test;
- dependency locks remain deterministic and hash-enforced.

Passing this PoC is evidence for **durable orchestration feasibility only**. It is not a production-readiness claim.
