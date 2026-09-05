# G1 Backup / Disaster Recovery Runbook

## Scope

This runbook covers the G1 Durable Core backup/DR baseline for AI Orchestra. It is repository and storage-provider agnostic.

The current local backup remains `scripts/backup.sh`: PostgreSQL custom-format dump + configuration + optional OpenCode state + Git bundles + internal `SHA256SUMS`. Secrets (`.env`, `.env.providers`, OpenCode `auth.json`) are intentionally excluded and must be recovered from a separate secret-management process.

## Commands

```bash
make backup
make backup-verify
make restore-drill
make backup-offsite
```

Each command accepts the newest local backup by default. The scripts also accept an explicit archive path when invoked directly.

## Local backup verification

`make backup-verify` performs fail-closed structural verification:

- archive is a regular `.tar.gz`, not a symlink;
- archive paths contain no absolute or `..` traversal entries;
- required PostgreSQL/configuration payload exists;
- every file listed in the internal `SHA256SUMS` matches;
- PostgreSQL dump is non-empty;
- `.env`, `.env.providers`, and `auth.json` are absent.

This proves archive integrity/structure. It does **not** prove recoverability by itself; `make restore-drill` is the recoverability test.

## Clean restore drill

`make restore-drill` never connects to the production PostgreSQL service. It:

1. verifies the selected backup;
2. creates an isolated Docker network and disposable PostgreSQL volume/container with no host port;
3. validates and restores `control-plane.pgdump` into that isolated database;
4. runs the **current checked-out Control Plane image** against the restored copy only;
5. executes `python -m app.schema_cli migrate` and `check` on the copy;
6. records the resulting Alembic revision and row counts for every public table;
7. destroys the disposable database/network/volume;
8. writes evidence under `backups/drills/restore-drill-*.json` with mode `600`.

Evidence includes:

- source backup path and SHA-256;
- start/end UTC;
- observed backup age at drill start;
- observed restore duration;
- pre/post migration revision;
- exact Git SHA;
- Docker image names and immutable local image IDs;
- restored table row counts.

`observed_restore_rto_seconds` is a measured drill restoration time. `observed_backup_age_seconds` is an observed local recovery-point upper-bound proxy when the selected archive is the newest successful backup. Neither value is a contractual RTO/RPO SLO until an automated backup schedule, off-host delivery cadence, alerting, and retention policy are activated and measured over time.

## Off-host contract

`make backup-offsite` is deliberately separate from `make backup`; local backup behavior does not silently acquire external side effects.

The export path is provider agnostic. Infrastructure exposes the remote/object-backed storage as `BACKUP_OFFSITE_DIR` and must independently guarantee:

1. storage is physically outside the production host/failure domain;
2. encryption at rest is enabled;
3. transport between production host and storage is authenticated and encrypted;
4. credentials permit create-only/least-privilege operation where the provider supports it;
5. retention/immutability/versioning prevents a compromised production host from trivially destroying all recovery points;
6. capacity/health failures are monitored outside AI Orchestra.

The script can verify bytes and local mount semantics, but it cannot cryptographically prove the provider's physical location or encryption policy. Therefore two explicit operator assertions are fail-closed prerequisites:

```text
BACKUP_OFFSITE_ENCRYPTION_AT_REST_CONFIRMED=yes
BACKUP_OFFSITE_AUTHENTICATED_TRANSPORT_CONFIRMED=yes
```

By default `BACKUP_OFFSITE_REQUIRE_MOUNTPOINT=1`, which requires the configured destination itself to be a distinct mountpoint. Set it to `0` only when the storage technology exposes a secure remote filesystem through a subdirectory and that exception has been independently reviewed.

The exporter:

- refuses destinations inside the project tree;
- refuses symlink destinations;
- refuses overwrite of an existing backup/manifest;
- copies through a temporary name;
- verifies SHA-256 before and after atomic rename;
- writes a JSON manifest beside the backup.

## Recovery sequence

For a real recovery:

1. obtain the chosen backup from off-host storage and verify its provider/object identity;
2. run `scripts/verify-backup.sh <archive>`;
3. recover secrets through the separate secret process — never from the backup archive;
4. build/pull the exact intended application images;
5. perform a clean `scripts/restore-drill.sh <archive>` first when time permits;
6. restore PostgreSQL to the replacement environment;
7. run the repository's migration CLI and schema check;
8. start Control Plane and dependent services;
9. run `make smoke`;
10. reconcile external effects/approvals before enabling future write-capable workflows.

The final reconciliation step is mandatory once durable external effects are introduced; a database restore alone must never be interpreted as proof that an external action may safely be replayed.

## Drill cadence

Before G1 acceptance, run at least one clean restore drill from the newest production backup and retain the evidence. After automation is enabled, run the drill periodically and after material schema/backup-format changes. A failed drill is a DR incident: do not delete the last known-good recovery point while investigating it.
