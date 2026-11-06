# Control Plane Schema Migration Runbook

## Purpose

Use this runbook to upgrade Control Plane PostgreSQL through the reviewed Alembic chain. Verify the intended head against the exact application image before deployment. [Current deployment and schema](PROJECT_STATUS.md#подтверждённые-версии)

`schema_cli migrate` handles the following cases under the existing database lock.

- For an empty database or a known versioned database, Alembic runs the applicable upgrades.
- For an unversioned database, the tool checks the complete schema shape. An exact current schema can be stamped at the head. The explicitly supported historical shapes `20260904_0001` and `20260917_0011` are stamped at their matching revision and then upgraded.
- Any unknown shape or drift stops migration. The tool verifies the resulting revision and schema before it commits.

## Invariants

- Never run manual `alembic stamp` in production.
- Never delete or recreate production tables to make the migration pass.
- Never set `SKIP_PRE_MIGRATION_BACKUP=1` in production.
- Runtime Control Plane is not an authorized schema migration path.
- Unknown schema state is a stop condition.
- Every scripted `docker compose run` disables pseudo-TTY allocation with `-T`, so
  the same reviewed commands work from an interactive shell, heredoc or CI runner.

## Deployment sequence

1. Record the current Git SHA, image identities, database revision and running service list. Build or select the approved images and review their exact Alembic head and restore test before the maintenance window. A branch that produces more than one Alembic head needs reconciliation before release.
2. Use the deployed Compose project and reviewed configuration files throughout the change. Set `COMPOSE_PROJECT_NAME` and `COMPOSE_FILE` for `make migrate` and `make schema-check` if the deployment uses an alternate project name or overrides. Include the Runner Manager overlay if that service is deployed. Retain existing configuration and credential files. For a new environment, `make init` can create the missing templates.
3. Pause submissions, drain active executions and wait for runner jobs to become terminal with cleanup confirmed. Stop the lifecycle writers that are running, including Runner Manager and OpenCode if present. Stop a subscription worker if one is connected. Keep PostgreSQL running.
4. Run the migration and check using the same deployment configuration.

   ```bash
   make migrate
   make schema-check
   ```

5. For a new workspace volume, run `docker compose run --rm -T --no-deps workspace-volume-init` before starting Workspace Manager. Start the approved service list explicitly with `docker compose up -d --no-deps`. For the cabinet and repository/workspace managers the service names are `control-plane`, `repo-manager` and `workspace-manager`. Restore Runner Manager and other services according to the recorded configuration. Check `docker compose ps`, the cabinet health endpoint, the actual image identities and the expected execution mode. Use the provider checks in `make smoke` only in an authorized OpenCode environment.

`make migrate` creates and verifies the project backup before any schema change and refuses to proceed while a lifecycle writer is running. Runtime writers must start after the migration and schema check succeed. Runtime startup checks the exact expected schema.

If a release changes runnerd or the disposable image, update the reviewed broker and pinned image before starting Runner Manager. Recreate Runner Manager after a runnerd restart to refresh its Unix socket bind mount. Check broker connectivity and container health.

## Expected result

For a reviewed image whose head is `20261010_0012`, a successful upgrade and check report the following.

```text
[OK] Schema migrated to 20261010_0012
[OK] Control Plane schema revision and shape: 20261010_0012
```

A database already at the correct head reports `Schema already at head`. For other approved releases, match the output against that image's head.

The Codex backend migration `20261010_0012` follows `20260917_0011`. It preserves existing executions as `backend=opencode` and adds the backend field for new runs. Enabling the subscription worker is a separate runtime change. [Codex runbook](CODEX_SUBSCRIPTION_LANE.md)

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

with the same project, configuration and image intended for rollout. Compare its head with the database revision and check whether either side is out of date.

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
4. keep downgrade under change control. The downgrade from `20261010_0012` refuses while Codex subscription history exists.
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
