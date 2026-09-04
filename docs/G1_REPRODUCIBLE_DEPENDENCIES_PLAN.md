# G1 Reproducible Dependencies — implementation plan

## Goal

Make the Python dependency closure used by Control Plane builds explicit, reviewable and fail-closed. A successful build must not silently resolve a different transitive dependency set on another day.

## Invariants

1. Human-edited dependency intent remains separate from machine-generated lock files.
2. Production and CI install from exact lock files, not open version ranges.
3. Lock files include hashes for downloadable Python artifacts where supported.
4. CI verifies locks are current and refuses drift.
5. Runtime image records the lock digest used for the build.
6. Mutable Docker/OS/npm inputs are tracked separately; this slice does not falsely claim a fully hermetic build.
7. No automatic dependency upgrade occurs during a normal application build.

## Scope

- introduce input requirements for runtime and development intent;
- generate exact runtime/dev lock files with hashes;
- install Control Plane image from runtime lock using `--require-hashes`;
- install CI test environment from dev lock using `--require-hashes`;
- add a lock verification command and CI gate;
- record dependency-lock SHA-256 inside the Control Plane image;
- document controlled update procedure.

## Follow-up hardening

Separate slices will pin:
- Docker base image digests;
- GitHub Actions revisions;
- apt/npm/toolchain closure;
- SBOM/provenance generation.

Those are required before we call the complete build path reproducible/hermetic.
