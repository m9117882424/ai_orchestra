# G3 Production Acceptance — 2026-09-17

## Accepted scope

G3 is production-accepted on main `8cea45110438636e266edc97ee87fd1babb0aa08` with Control Plane schema `20260916_0009`.

Accepted capabilities:
- G3.1 disposable execution sandbox with no network, no Docker socket and no production/provider/Git secrets;
- G3.2 durable `runner_jobs` with lease/fencing, recovery, trust recheck, exact runner image pin and cleanup evidence;
- G3.3 automatic runner-gated workflow for project executable checks; direct project `bash` is denied to agents;
- exact source-snapshot binding before and after disposable copy;
- fail-closed completion for changed workspaces without successful runner evidence;
- one-shot machine-only checkpoint format repair without weakening the strict checkpoint parser; invalid or repeated malformed checkpoints remain fail-closed.

## CI and rollout evidence

- PR #27: G3 runner-gated workflow; merged as `3b21680d3117830a4da3a6342063a5596dc0e4fe`; PR and post-merge CI green.
- PR #28: wrapped-checkpoint production hotfix and runtime source-readability hardening; merged as `8cea45110438636e266edc97ee87fd1babb0aa08`.
- PR #28 CI run #396: all 19 gates green.
- post-merge CI run #397: all 19 gates green.
- Documentation closure PR #29 merged as `775019c5d0814c5a63e4ff0f4922ce6c196f2eac`;
  [post-merge CI #400](https://github.com/m9117882424/ai_orchestra/actions/runs/35188890168)
  completed successfully at 2026-09-17 06:17:49 UTC. Every substantive job step passed.
- production smoke after final rollout: Model Gateway 5/5, schema check green, 9 runtime services healthy, `runnerd` active.
- production disposable runner image: `sha256:235bc849835769854af3d08557f616e0294b8207b9ef7846147595b65d4a7c8e`.

## Final production E2E acceptance

Task: `233d7e39-edc3-4c51-a019-7fc79ce2acd4`

Execution: `67080907-2a58-4678-8a75-559aa15bd940`

Workspace: `6cba632e-a75f-45b5-ae0d-3a93eeaf026f`

Immutable base commit: `8cea45110438636e266edc97ee87fd1babb0aa08`

Observed path:
1. Lead created exactly one workspace-only file: `docs/G3_3_FINAL_ACCEPTANCE.txt`.
2. Lead initially wrapped a valid checkpoint with explanatory text.
3. Worker rejected execution of that wrapped response and issued the one-shot machine-only format repair prompt.
4. Lead returned a standalone checkpoint without changing commands.
5. Worker enqueued two durable runner jobs bound to exact source snapshot `d58c40def50f4823039aa943d93b2c69566c1d1a2eb04465c76543850d21a5a1`.
6. `diffcheck` job `a0c4802f-a59b-4062-9591-e22448c1fa4f` completed with exit code 0 and `cleanup_confirmed=true`.
7. `content` job `16c4e27d-f687-4ed4-a3fd-a2df95c4dcc0` completed with exit code 0 and `cleanup_confirmed=true`.
8. Both jobs used the exact pinned runner image and no disposable runner container remained after completion.
9. Runner evidence returned to Lead; QA/reviewer delegation completed; execution reached `completed / manager_review`.
10. Workspace inspection retained exactly one changed file, while Git HEAD and tree stayed at the immutable base repository state.

## Backup evidence

Pre-rollout backup:
- `ai-orchestra-20260917T055217Z.tar.gz`
- SHA-256 `406b4c2fda8ea4323a16324b20064014ca686f68b07403b798a688f1210df7a2`
- format 3, verified.

Post-acceptance backup:
- `ai-orchestra-20260917T060514Z.tar.gz`
- SHA-256 `e20552739d015b1ba2d39601aae0134b55fba24c7a6762505b2e9f5e64c34b2a`
- format 3, verified and includes the accepted runner-job evidence/workspace state.

## Explicit non-goals / next gate

G3 does **not** grant Git push/PR/deploy autonomy to agents and does not expose production/provider/Git credentials to the runner. Detailed per-child-run durable telemetry, result packages and artifact provenance are the next G4 scope. Temporal remains PoC-only and is not part of the production topology.
