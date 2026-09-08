# G1 Production Acceptance Record — 2026-09-07

## Decision

G1 Durable Core is accepted for the current AI Orchestra pilot scope. The decision
is bound to the source, image, schema and backup identifiers below. It does not
admit Temporal into production, grant Git or deploy autonomy, or satisfy any G2+
gate.

## Bound identifiers

| Evidence | Observed value |
| --- | --- |
| Previous production source | `e00b7cdfbf52c73c05e71c6bc0c921f6bdd5c0b4` |
| Accepted production source | `05bafd920c803cf0bfba42de421512dd33eb983c` |
| Review | [PR #18](https://github.com/m9117882424/ai_orchestra/pull/18) |
| CI | [workflow run 34124515584](https://github.com/m9117882424/ai_orchestra/actions/runs/34124515584), successful |
| Control Plane image identity | `sha256:28f0c809271075ab6b9301a863b540ac86c405c3db840e61a92c228d303b3914` |
| Schema transition | `20260905_0003` -> `20260907_0004` |
| Migration backup | `ai-orchestra-20260907T144332Z.tar.gz` |
| Backup SHA-256 | `3f3ea7134112ea497c246be146a0f53d9c7f373c5b4324861bceede078ebc36c` |
| Backup size | `381671` bytes |

The backup verifier confirmed safe archive paths, required payloads, internal file
checksums, a non-empty PostgreSQL dump and absence of `.env`, `.env.providers` and
OpenCode `auth.json`.

## Observed rollout results

1. The existing `0003` physical schema passed the old-image schema check.
2. Pre-deploy smoke passed all five required model aliases plus OpenCode, Control
   Plane, PostgreSQL and the existing Execution Worker.
3. Source advanced by fast-forward from `e00b7cd` to `05bafd9`; the `0.6.1`
   Control Plane image was built before the lifecycle writers stopped.
4. `control-plane` and `execution-worker` were stopped while PostgreSQL, OpenCode,
   Model Gateway and Model Router remained available.
5. The verified backup above was taken immediately before schema mutation.
6. Alembic completed the transactional `0003 -> 0004` upgrade, and the new image
   verified revision plus physical schema shape before application startup.
7. `control-plane` and `execution-worker` started from image `0.6.1` and both became
   healthy.
8. Post-deploy smoke passed the five required model aliases, credential rejection,
   model catalog, OpenCode, Control Plane, PostgreSQL and Execution Worker health.

No rollback was required.

## Operational finding and closure

The accepted commit's `make migrate` and `make schema-check` commands requested a
pseudo-TTY implicitly. When invoked through an SSH heredoc, Compose rejected the
non-TTY stdin even though the schema check itself had completed. The rollout used
the semantically identical commands with explicit `docker compose run -T`.

The follow-up change containing this record adds `-T` to every scripted Compose
run and makes that property a static validation invariant. This operational defect
did not affect the running `0.6.1` services or migrated data.

## Evidence limitation

This record transcribes owner-authorized operator output and repository/CI state.
It is useful pilot evidence but is not a signed deployment attestation or immutable
WORM record. Content-addressed release evidence remains a G4/G6 capability.

## Next gate

G2 Repository & Workspace Platform is next: Repository Registry, trusted Repo
Manager, task worktrees and mandatory workspace preflight before model inference.
