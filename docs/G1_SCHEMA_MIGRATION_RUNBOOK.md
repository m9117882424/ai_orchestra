# G1 Control Plane Schema Migration Runbook

## Purpose

This runbook is the only supported path for moving the existing Control Plane PostgreSQL database onto the Alembic baseline introduced in G1.

The first migration is special because production already contains tables created historically by SQLAlchemy `Base.metadata.create_all()`.

The migration tooling therefore supports two fail-closed paths:

1. **fresh database** — Alembic creates the complete schema;
2. **legacy existing database** — the tool verifies that tables, columns, types/nullability, primary keys, foreign keys and explicit indexes match the declared ORM baseline, then stamps the database at the baseline revision without changing application data.

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

docker compose build control-plane opencode

docker compose up -d postgres

make migrate

make schema-check

docker compose up -d --no-deps control-plane
docker compose up -d --no-deps opencode

docker compose ps
make smoke
```

`make migrate` creates the normal project backup before any schema action.

## Expected first production migration

For the current production database the expected message is equivalent to:

```text
[OK] Existing schema verified and stamped at 20260904_0001; data unchanged
```

This means the legacy database shape matched the migration baseline and only `alembic_version` metadata was added.

If the database is already migrated, the expected message is:

```text
[OK] Schema already at head: 20260904_0001
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

The initial baseline migration does not alter business data in the existing database; it only adds Alembic revision metadata after schema verification.

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
