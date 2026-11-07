# AI Orchestra: assured read-only review (opt-in)

Status: **implementation in development; not a production enablement record**.
Depends on subscription-assurance preflight PR #57 and database migration `20261107_0013`.

## Scope

The `opencode-review` backend is exclusively for `general-high-assurance` **static review** of a
specified Git branch head. It does not run tests, change source files, delegate to development
agents, commit, push, deploy, or approve business changes. It is not eligible for
`regulated-critical` repositories. General-purpose `opencode` remains disabled unless its
own independent opt-in is enabled; subscription execution remains `general-standard` only.

The request must carry both `source_branch` and the exact `expected_commit` (full SHA-1 or SHA-256).
At workspace preparation, Workspace Manager verifies that the trusted mirror's
`refs/remotes/origin/<source_branch>` resolves to exactly `expected_commit`. A missing branch
or a moved PR head fails closed; it never falls back to repository `main`.
The source tree and worktree identity are rechecked before inference and before review completion.

## Isolation and evidence

- An optional Compose overlay supplies `opencode-review` and `assured-review-worker`; **neither
  is present in the base Compose deployment**. No host port or Docker socket is exposed.
- The OpenCode reviewer uses a separate configuration and state, a read-only root filesystem,
  a read-only task-workspaces volume, no Linux capabilities, and the internal Model Gateway
  network only. All mutation, shell, delegation, external-directory and web tools are denied.
- A dedicated worker leases only `opencode-review` rows; the standard worker never leases them.
  Both respect the existing DB fencing, deadline, cancellation and repository trust gates.
- A review result must have a parseable single `AI_ORCHESTRA_VERDICT` object. `pass` means no
  reported findings, `fail` means the reviewer reported defects, and `blocked` means it cannot
  review. A completed **review execution** does not constitute approval to merge the PR.
- Source tree must remain unchanged. The worker records model provider, model ID and positive
  input/output tokens from **server-observed assistant message metadata**, not a guessed
  subscription cost or empty session summary. Missing metering/verdict/source identity fails
  the run; the Result Package includes the independent reviewer verdict and source branch/SHA.
- Provider cost may remain unknown even when model tokens are confirmed. Display unknown cost
  as unknown; do not invent a zero-cost claim. No raw model prompt, secrets or code are stored in
  usage/evidence rows.

## Controlled deployment (not yet executed)

1. Review and merge PR #57 and this PR in that order; verify green CI and apply migration
   `20261107_0013`. Back up PostgreSQL first. Confirm existing taxi/Arvento services are not
   affected by Model Router startup and that Docker network/address capacity is sufficient.
2. Validate `docker compose -f docker-compose.yml -f compose.assured-review.yml config` and
   the isolation checks in `scripts/static_security_check.py`. Confirm the Model Gateway's
   `orchestra-reviewer` provider alias is functional and a bounded spending budget exists.
3. Set `CONTROL_PLANE_ALLOW_HIGH_ASSURANCE_REVIEW=true` **in the isolated deployment
   environment** (not in git). Start only the necessary services using both Compose files,
   explicitly listing the desired service names. Do not start unrelated disabled workers.
4. Readiness: check private `opencode-review` health, `assured-review-worker` heartbeat,
   DB migration head, model routing and current registry status before creating a run.
5. Create a development-domain task assigned to the Arvento registry (`assurance_tier` remains
   `general-high-assurance`). Launch a **pinned** review request:

   ```json
   {
     "backend": "opencode-review",
     "source_branch": "feature/efficiency-report-speed-controls",
     "expected_commit": "602980e28b56f7ac73b0f41c60fe9cedf63e5b2a"
   }
   ```

   Use `POST /api/tasks/<task_id>/execute` with the usual manager authentication and
   change-control headers. Reconfirm the live PR head before execution; the displayed SHA
   above is a snapshot from 07.11.2026, not an assertion that the remote branch is unchanged.
6. On completion inspect the durable Result Package, source SHA/branch, QA verdict,
   token evidence and server audit. Zero findings are not proof of runtime correctness;
   normal CI, independent test review and human release approval remain mandatory.

## Acceptance tests and limitations

Unit/integration tests exercise disabled-by-default behavior, tier and pinning rejections,
immutable branch-head verification, distinct worker backends, read-only verdict schema,
zero/missing metering, dirty-source rejection and idempotent message metering.

Remaining operational verification: the optional Compose overlay has not been booted against
the actual Model Router/OpenCode version or PRIMARY server. The read-only workspace volume
contains multiple prepared workspaces; per-task path access is additionally restricted by
OpenCode's `external_directory` permission, but this is not a separately mounted namespace
per task. For stricter tenant separation, provision one filesystem-mounted workspace per
execution before extending this lane to unrelated repositories.
