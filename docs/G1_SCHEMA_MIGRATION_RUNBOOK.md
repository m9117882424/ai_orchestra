# Control Plane Schema Migration Runbook

## Purpose

This runbook is the supported path for moving the Control Plane PostgreSQL database through its reviewed Alembic chain. The accepted G3 schema is `20260916_0009` (G3.2 adds `0008` runner jobs; G3.3 adds `0009` checkpoint/snapshot bindings).

The first migration is special because production already contains tables created historically by SQLAlchemy `Base.metadata.create_all()`.

The migration tooling therefore supports two fail-closed paths:

1. **fresh database** — Alembic creates the complete schema;
2. **legacy existing database** — the tool verifies that tables, columns, types/nullability, primary keys, foreign keys, named checks and explicit indexes match the declared historical `20260904_0001` shape, stamps that exact revision, and upgrades through `0002` (lease/fencing), `0003` (durable queued dispatch), `0004` (deadline/cancellation intent), `0005` (Repository Registry), `0006` (trusted Repo Manager synchronization state), and `0007` (durable task workspaces and execution contract v2).

If legacy schema differs, the tool refuses to stamp it.

## Invariants

- Never run manual `alembic stamp` in production.
- Never delete or recreate production tables to make the migration pass.
- Never set `SKIP_PRE_MIGRATION_BACKUP=1` in production.
- Runtime Control Plane is not an authorized schema migration path.
- Unknown schema state is a stop condition.
- Every scripted `docker compose run` disables pseudo-TTY allocation with `-T`, so
  the same reviewed commands work from an interactive shell, heredoc or CI runner.

## Deployment sequence

From `/opt/ai_orchestra` after updating the repository:

For the accepted G3 topology, first finish or explicitly cancel active executions
and wait for all runner jobs to become terminal with cleanup confirmed. Do not
stop runnerd while disposable jobs are active. Pause operator submissions for
the maintenance window. The Runner Manager overlay must use the existing
`RUNNERD_SOCKET_GID` and `RUNNERD_SOCKET_HOST_PATH` configuration.

```bash
make init
make preflight
make build
docker compose -f docker-compose.yml -f deploy/docker-compose.runner-manager.yml build runner-manager
docker compose up -d postgres
docker compose stop control-plane execution-worker repo-manager workspace-manager opencode
docker compose -f docker-compose.yml -f deploy/docker-compose.runner-manager.yml stop runner-manager
make migrate
make schema-check
make up
docker compose -f docker-compose.yml -f deploy/docker-compose.runner-manager.yml up -d --no-deps runner-manager
docker compose ps
make smoke
```

`make init` creates the new ignored `.env.repositories` scope when upgrading from
G2.1. Preserve an existing file; never reconstruct private Git credentials from a
backup or terminal transcript.

`make migrate` creates and verifies the normal project backup before any schema
action. A failed archive/path/checksum verification stops migration. The old
Control Plane is stopped first so it cannot write lifecycle state while the schema
and application image move together. `make migrate` refuses to proceed while any
lifecycle writer, including Runner Manager, is running. Model Router and Gateway may remain
available during this maintenance window.

If this release changes runnerd or sandbox source, update the reviewed host broker
and build/pin the new disposable image before starting Runner Manager. This is a
separately authorized runtime rollout, not an implicit effect of `make build`.
After restarting runnerd, recreate Runner Manager: its file bind mount can retain
the old Unix socket inode. Verify broker connectivity as well as container health.

Do not reverse `make migrate` and `make up`: `control-plane`, `execution-worker`,
`repo-manager` and `workspace-manager` verify the exact schema at startup and
must fail closed on an old revision.

## Expected first production migration

For a versioned `20260904_0001` database the expected message is equivalent to:

```text
[OK] Schema migrated to 20260916_0009
```

For an unversioned database that exactly matches the historical baseline:

```text
[OK] Historical baseline 20260904_0001 verified, migrated to 20260916_0009; data unchanged
```

Active executions present during `0004` receive a fresh two-hour deadline grace
period. Terminal execution history receives no deadline and is otherwise unchanged.
Migration `0005` creates a repository registry. Migration `0006` preserves
registry identity/policy, revokes pre-worker operational trust by setting rows to
`pending_validation`, and queues enabled rows for trusted synchronization.
Migration `0007` adds nullable repository binding to existing tasks, creates an
empty workspace registry and preserves every existing execution as contract v1;
only newly requested executions use contract v2.

If the database is already migrated, the expected message is:

```text
[OK] Schema already at head: 20260916_0009
```

## Failure: legacy schema mismatch

Example:

```text
[FAIL] Legacy database does not match the declared baseline; refusing stamp:
 - ...
```

Required response:

1. stop deployment;
2. do not stamp manually;
3. preserve the backup created immediately before the attempt;
4. collect the complete mismatch output;
5. compare the production schema with the declared migration and ORM model;
6. resolve the discrepancy through a reviewed migration or a corrected baseline;
7. rerun the migration tests before another production attempt.

## Failure: application refuses schema revision

The production runtime verifies that the current Alembic revision equals the repository head. A mismatch prevents startup.

Do not bypass the check. Run:

```bash
make schema-check
```

and determine whether the database or deployed application image is out of date.

## Runtime DDL protection

After a valid migration the normal Control Plane SQLAlchemy engine rejects `CREATE`, `ALTER`, `DROP`, `TRUNCATE` and `COMMENT ON` statements.

This is defense in depth. The long-term G1/G2 target is a separate database role for migrations so the application runtime database role has no DDL privileges at PostgreSQL level either.

## Rollback

The initial baseline stamp does not alter business data. Later G1 revisions add
execution lifecycle columns and relax `opencode_session_id` nullability; `0004`
backfills deadlines only for active rows. G2 revision `0005` adds a new table;
`0006` adds synchronization metadata and deliberately requires Registry rows to be
revalidated before `ready` can be trusted. `0007` adds workspace state and
immutable execution binding while retaining legacy runs under contract v1.

If application rollout fails after a successful migration:

1. keep all lifecycle writers stopped and preserve the verified pre-migration backup;
2. do not start an older application image against a newer revision — runtime schema guards will reject it;
3. prefer a reviewed fix-forward on the new schema;
4. do not run Alembic downgrade automatically;
5. if rollback is explicitly approved before writers resume, restore the verified pre-migration backup and then restore the matching application/container revision;
6. retain the failed database separately for investigation rather than overwriting it in place.

## Verification evidence

For each production migration retain:

- Git commit SHA;
- Control Plane image identity/digest;
- pre-migration backup filename and checksum;
- migration command output;
- `make schema-check` output;
- `docker compose ps` output;
- smoke-test result;
- operator and timestamp.

The first accepted pilot rollout is recorded in
[`G1_PRODUCTION_ACCEPTANCE_2026-09-07.md`](G1_PRODUCTION_ACCEPTANCE_2026-09-07.md).
