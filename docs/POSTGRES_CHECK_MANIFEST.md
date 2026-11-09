# PostgreSQL CHECK manifest verification

`control_plane/app/check_constraints_postgresql.json` is an exact SQLAlchemy
`Inspector.get_check_constraints()` `sqltext` manifest for fresh ORM/Alembic
creation. `check_constraints_postgresql_restored.json` records 16 additional
exact Inspector strings after PostgreSQL deparses CHECK definitions for
`pg_dump` and reparses them during `pg_restore`. It is **not** raw
`pg_get_constraintdef` output: SQLAlchemy removes the `CHECK (...)` wrapper
and can remove an additional parenthesis pair. The manifest has 58 named
checks. The head backend CHECK must include `opencode-review`; revision 0012
must have only `opencode` and `codex-subscription`. The restored 0012 backend
has a separate exact alternative in `schema.py`.

The supported contract is PostgreSQL 14 and 16 with SQLAlchemy 2.0.52. The
application rejects other PostgreSQL majors and other SQLAlchemy versions
for CHECK shape verification. Each named constraint accepts only its committed
fresh string and, where present, its committed restored string. There is no
SQL normalization. `NOT VALID` and `NO INHERIT` options are rejected. A new
model CHECK name requires a manifest update and a passing matrix before release.

The restored alternatives were captured from disposable PostgreSQL 14.24
single-user mode: create the current ORM schema, obtain
`pg_get_constraintdef(oid)`, re-add each named CHECK from that definition, then
read `pg_get_constraintdef(oid, true)` using SQLAlchemy's Inspector wrapper
stripping. Exactly 16 of 58 strings changed, matching the reported CI drift
count. The change is an ARRAY cast rewrite from `ARRAY[...::character varying]::text[]`
to `ARRAY[...::character varying::text]`, including nested status checks.
The PG16 `pg_dump`/`pg_restore` matrix remains the required independent proof.

Reproduce against a **disposable PostgreSQL database** with a role that can
create schemas:

```sh
python3 -m pip install --require-hashes --requirement control_plane/requirements-dev.lock
POSTGRES_CHECK_TEST_URL='postgresql+psycopg://check_matrix:ci-check-only@127.0.0.1:5432/check_matrix' \
  python3 -m pytest -q control_plane/tests/test_postgres_check_matrix.py
```

Run that command once on PostgreSQL 14 and once on PostgreSQL 16, or use the
`postgres-check-matrix` job in `.github/workflows/validate.yml`. The test
creates random isolated schemas and drops them afterward. It creates the head
through both ORM `metadata.create_all` and Alembic, captures the **actual**
inspector rows, and requires both complete name sets and every `sqltext` to
equal the committed fresh manifest. It also dumps and restores a current-head
and 0012 schema through real `pg_dump`/`pg_restore`, requires the exact
restored Inspector rows, then exercises migration and malformed CHECK refusals.
A mismatch fails CI; there is no
automatic manifest rewrite from a database. Review any proposed expression
change against the ORM and migration source before editing the manifest.

Until the full matrix and backup/restore smoke pass on both supported server
majors where applicable, PostgreSQL CHECK runtime compatibility and the
release gate remain **UNVERIFIED / NO-GO**.
