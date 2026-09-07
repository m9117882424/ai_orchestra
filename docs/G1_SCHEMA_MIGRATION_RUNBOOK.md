# G1 Control Plane Schema Migration Runbook

## Purpose

This runbook is the only supported path for moving the existing Control Plane PostgreSQL database through the G1 Alembic chain. The current repository head is `20260907_0004`.

The first migration is special because production already contains tables created historically by SQLAlchemy `Base.metadata.create_all()`.

The migration tooling therefore supports two fail-closed paths:

1. **fresh database** — Alembic creates the complete schema;
2. **legacy existing database** — the tool verifies that tables, columns, types/nullability, primary keys, foreign keys and explicit indexes match the declared historical `20260904_0001` shape, stamps that exact revision, and upgrades through `0002` (lease/fencing), `0003` (durable queued dispatch), and `0004` (deadline/cancellation intent).

If legacy schema differs, the tool refuses to stamp it.

## Invariants

- Never run manual `alembic stamp` in production.
- Never delete or recreate production tables to make the migration pass.
- Never set `SKIP_PRE_MIGRATION_BACKUP=1` in production.
- Runtime Control Plane is not an authorized schema migration path.
- Unknown schema state is a stop condition.

## Deployment sequence

From `/opt/ai_orchestra` after updating the repository:

```bash
make preflight
make build
docker compose up -d postgres
docker compose stop control-plane execution-worker
make migrate
make schema-check
make up
docker compose ps
make smoke
```

`make migrate` creates the normal project backup before any schema action. The old
Control Plane is stopped first so it cannot write lifecycle state while the schema
and application image move together. Model Router, Gateway and OpenCode may remain
available during this maintenance window.

Do not reverse `make migrate` and `make up`: both `control-plane` and
`execution-worker` verify the exact schema at startup and must fail closed on an old
revision.

## Expected first production migration

For a versioned `20260904_0001` database the expected message is equivalent to:

```text
[OK] Schema migrated to 20260907_0004
```

For an unversioned database that exactly matches the historical baseline:

```text
[OK] Historical baseline 20260904_0001 verified, migrated to 20260907_0004; data unchanged
```

Active executions present during `0004` receive a fresh two-hour deadline grace
period. Terminal execution history receives no deadline and is otherwise unchanged.

If the database is already migrated, the expected message is:

```text
[OK] Schema already at head: 20260907_0004
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
backfills deadlines only for active rows.

If application rollout fails after a successful baseline stamp:

1. keep the database and backup intact;
2. roll the application/container revision back;
3. do not run Alembic downgrade automatically;
4. investigate the application failure;
5. if a future migration changed data/schema, use that migration's reviewed rollback plan rather than a generic downgrade command.

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
