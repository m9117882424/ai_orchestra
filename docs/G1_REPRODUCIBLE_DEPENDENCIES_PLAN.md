# G1 Reproducible Dependencies — implementation plan

## Goal

Make the Python dependency closure used by Control Plane builds explicit, reviewable and fail-closed. A successful build must not silently resolve a different transitive dependency set on another day.

## Invariants

1. Human-edited dependency intent remains separate from machine-generated lock files.
2. Production and CI install from exact lock files, not open version ranges.
3. Lock files include hashes for downloadable Python artifacts where supported.
4. CI verifies locks are current and refuses drift.
5. Runtime image records the lock digest used for the build.
6. GitHub Actions used by the G1 validation and lock workflows are pinned to exact commit SHAs.
7. Mutable Docker/OS/npm inputs are tracked separately; this slice does not falsely claim a fully hermetic or bit-for-bit reproducible build.
8. No automatic dependency upgrade occurs during a normal application build.

## Scope

- introduce input requirements for runtime and development intent;
- generate exact runtime/dev lock files with hashes;
- install Control Plane image from runtime lock using `--require-hashes`;
- install CI test environment from dev lock using `--require-hashes`;
- add a lock verification command and CI gate;
- generate candidates in a sanitized environment with an explicit package index and no inherited pip configuration;
- verify the lock generator on push and pull request when dependency inputs, locks or the generator workflow change;
- pin GitHub Actions revisions used by the G1 validation and lock workflows;
- record dependency-lock SHA-256 inside the Control Plane image;
- document controlled update procedure.

## What this slice proves

This slice proves the Python dependency closure is explicit, hash-checked and regenerated deterministically from declared inputs under the documented generator policy. The ordinary CI Docker build remains a **buildability/current-upstream** check, not evidence of a hermetic or bit-identical build.

## Follow-up hardening

Separate slices will address:
- Docker base image digests;
- apt/npm/toolchain closure;
- reproducible build environment/containerization for lock generation and build steps;
- SBOM and provenance generation;
- broader supply-chain policy for any future third-party GitHub Actions.

Those are required before we call the complete build path reproducible/hermetic.
