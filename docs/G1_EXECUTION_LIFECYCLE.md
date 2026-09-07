# G1 Durable Execution Lifecycle

## Scope

This document defines the repository-side G1 lifecycle for development executions.
It does not grant Git write, external-write, secret, financial, or production-deploy
authority. Temporal remains an isolated durability PoC; the production execution
lifecycle currently uses PostgreSQL state plus a dedicated `execution-worker`.

## Durable state machine

1. `POST /api/tasks/{task_id}/execute` commits an `ExecutionRun` in
   `queued / dispatch_pending` before calling OpenCode.
2. `execution-worker` claims the row with a PostgreSQL lease and monotonically
   increasing `lease_generation`.
3. Dispatch reconciles an OpenCode session by execution metadata and uses
   deterministic message and text-part IDs before moving the run to `running`.
4. The worker, not the browser, observes OpenCode and commits the result.
5. A completed execution moves the task to `qa`.

The browser only reads execution state and progress. The legacy production
compatibility wrapper rewrites cached `POST /refresh` requests to a read-only list
request; the core API has no mutating refresh endpoint.

## Failure and recovery invariants

- An expired lease can be claimed by another worker.
- Every database mutation verifies owner, unexpired lease, and exact generation.
- A stale worker cannot persist a session, dispatch transition, result, timeout, or
  cancellation outcome.
- A lost create-session response is reconciled by
  `ai_orchestra_execution_id` metadata.
- A lost prompt response is reconciled by deterministic OpenCode message and
  text-part IDs, so retrying the same logical dispatch cannot duplicate either
  the turn or its prompt body.
- Ambiguous session recovery fails closed; no new prompt is sent.
- OpenCode/provider errors retain an active, recoverable execution instead of
  reporting false success.

OpenCode `v1.18.27` does not expose a caller-supplied session ID on the public
`POST /session` contract. A stale create call can therefore leave an empty orphan
session, but fencing prevents that session from receiving a prompt. Prompt delivery
itself uses deterministic message and text-part IDs and is reconciled before retry.

This recovery contract was checked against the pinned upstream implementation:
public [`Session.create`](https://github.com/anomalyco/opencode/blob/v1.18.27/packages/opencode/src/session/session.ts),
[`PromptInput` and prompt loop](https://github.com/anomalyco/opencode/blob/v1.18.27/packages/opencode/src/session/prompt.ts),
and the message/part
[`projector`](https://github.com/anomalyco/opencode/blob/v1.18.27/packages/core/src/session/projector.ts).
An OpenCode version change must revalidate these assumptions before rollout.

## Deadline semantics

Every new run receives immutable `deadline_at` from
`CONTROL_PLANE_EXECUTION_TIMEOUT_SECONDS` (default: 7200 seconds; allowed range:
60 seconds to 7 days). Migration `20260907_0004` gives already-active runs a fresh
two-hour grace period; terminal history remains unchanged.

When the deadline is reached:

1. the worker resolves every known OpenCode session for the execution;
2. it renews and verifies the exact lease immediately before each abort;
3. only a confirmed abort or `404 Not Found` permits `failed / timed_out`;
4. an uncertain abort remains `running / timeout_abort_pending` and is retried.

This prevents the database from claiming a terminal timeout while external work
may still be running.

## Cancellation semantics

The manager API only commits durable cancellation intent. It immediately increments
the generation and releases the old lease; it does not perform an external side
effect in the HTTP request.

The next worker generation performs cleanup:

- a queued session is aborted, deleted, then aborted again across the delete
  boundary, because a prompt may have been accepted before its dispatch transition
  was committed;
- a running session is aborted so its messages remain available;
- uncertain cleanup remains active as `cancel_cleanup_pending` and retries;
- only confirmed cleanup permits terminal `cancelled / stopped`.

The operation is idempotent. Repeating the manager request does not create another
intent or generation.

## Liveness and observability

The worker updates `/tmp/ai-orchestra-execution-worker.heartbeat` from its main
loop. Docker marks the service unhealthy if the file is absent or older than three
minutes. Production smoke requires the container to be both running and healthy.

Manager responses expose:

- `heartbeat_at`;
- `deadline_at`;
- `cancel_requested_at`;
- `lease_generation`;
- current stage and durable error.

OpenCode progress failure is reported as `session_state=unavailable` without
changing the durable lifecycle.

## Required deployment order

The application image and database revision must move together. For an existing
installation, follow [`G1_SCHEMA_MIGRATION_RUNBOOK.md`](G1_SCHEMA_MIGRATION_RUNBOOK.md):
build the target image, stop the old Control Plane lifecycle writer, run the backed-up
Alembic migration, then start `control-plane` and `execution-worker` and run smoke.
Starting the new services before migration is a stop-condition violation.

## Verification

The unit/migration suite covers queued persistence, session/prompt recovery,
expired-lease recovery, zombie fencing, browser-independent completion, timeout
abort retry, cancellation cleanup retry, stale cancellation races, schema adoption,
and active-row deadline backfill.

Docker/CI additionally covers Compose resolution, secret/network boundaries,
container buildability, PostgreSQL migration semantics, backup/restore DR, Temporal
PoC regression, worker health, and the OpenCode toolchain.

## Remaining boundary after G1

G1 makes the existing development execution lifecycle recoverable; it does not make
the platform repository-safe. The next gate is G2: Repository Registry, a trusted
Repo Manager, Git URL hardening, task-specific worktrees, and workspace preflight
before the first model call. Git credentials must remain outside OpenCode.
