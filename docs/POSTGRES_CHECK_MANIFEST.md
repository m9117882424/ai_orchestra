# PostgreSQL CHECK manifest verification

`control_plane/app/check_constraints_postgresql.json` is an exact SQLAlchemy
`Inspector.get_check_constraints()` `sqltext` manifest. It is **not** raw
`pg_get_constraintdef` output: SQLAlchemy removes the `CHECK (...)` wrapper
and can remove an additional parenthesis pair. The manifest has 58 named
checks. The head backend CHECK must include `opencode-review`; revision 0012
must have only `opencode` and `codex-subscription`.

The supported contract is PostgreSQL 14 and 16 with SQLAlchemy 2.0.52. The
application rejects other PostgreSQL majors and other SQLAlchemy versions
for CHECK shape verification. Expressions are compared exactly; `NOT VALID`
and `NO INHERIT` options are rejected. A new model CHECK name requires a
manifest update and a passing matrix before release.

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
equal the committed manifest. It separately checks Alembic 0012, head
stamping versus 0012 migration, malformed CHECK rejection, stale versioned
0013 refusal, and reflected CHECK options. A mismatch fails CI; there is no
automatic manifest rewrite from a database. Review any proposed expression
change against the ORM and migration source before editing the manifest.

Until the exact-head matrix passes on both server majors, PostgreSQL CHECK
runtime compatibility and the release gate remain **UNVERIFIED / NO-GO**.
