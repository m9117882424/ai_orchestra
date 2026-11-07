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

### OpenAI-only profile (no third-party provider)

`compose.assured-openai.yml` optionally replaces the Model Router's catalog with
`config/model-router.assured-openai.yaml`: **only** the `orchestra-reviewer` alias is
available and it resolves to the official OpenAI API model `gpt-6.1-sol`.
The API credential is read exclusively by Model Router from the untracked
`.env.providers` (`OPENAI_API_KEY`); OpenCode and the review worker receive no
provider credential. This is a **review-only isolated staging profile**. Never
layer it over a running general-purpose Orchestra deployment: other agent aliases
are intentionally absent and would stop resolving.

Eligible ChatGPT Pro subscriptions may claim OpenAI API credits by linking their
subscription to an owned API organization in ChatGPT Settings → Billing. The
benefit must be explicitly claimed and its remaining credits checked; ChatGPT
subscription access alone does **not** make arbitrary API requests free. Prior to
the first review, verify the project API key's model access and that the provider
organization cannot continue into paid purchased credits/auto-recharge without
the owner's approval. Do not store credentials in git, prompts, screenshots or
execution evidence.

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
2. Validate `docker compose -f docker-compose.yml -f compose.assured-review.yml
   -f compose.assured-openai.yml config` and the isolation checks in
   `scripts/static_security_check.py` **only on the review-only staging deployment**.
   Confirm the Model Gateway's `orchestra-reviewer` OpenAI alias is functional,
   subscriber API credits are active and further chargeable usage is blocked by
   the organization's billing configuration. A dry config check must not dump
   provider secret values to logs or chat. Do not attempt Docker's automatic
   bridge address allocation on PRIMARY while the default pools are exhausted.

   If the PRIMARY's automatic address pools are exhausted, an **optional,
   not-yet-deployed** `compose.assured-staging-network.yml` defines six separate
   staging-only `/27` bridge networks within `172.23.0.0/24`. This does **not**
   authorize creating them. Before a controlled GO, check `ip -4 route`, all
   existing Docker network IPAM subnets and private/VPN route plans for overlap;
   confirm an independent `-p ai-orchestra-review-staging` project and separate
   credentials, volumes and loopback ports. Never reuse empty networks owned by
   the live Orchestra, CRM or other systems. Creation will change Docker bridge
   and iptables state even without touching an existing container; stop if an
   approval, routing, resource or network-isolation gate is not satisfied.

   When the explicit network GO is granted, append
   `-f compose.assured-staging-network.yml` **last** in staging commands. Use
   stage-only `CONTROL_PLANE_PORT` and `MODEL_GATEWAY_PORT` values to prevent
   loopback port collisions. The same overlay also pins a distinct
   `ai-orchestra-assured-staging/*` image namespace: Compose project names
   alone do not isolate images tagged explicitly in the base file. Do not
   overwrite or retag production's images. Never issue a blanket
   `docker compose up`.
3. Set `CONTROL_PLANE_ALLOW_HIGH_ASSURANCE_REVIEW=true` **in the isolated deployment
   environment** (not in git). Start only the necessary services using all three Compose files,
   explicitly listing the desired service names. Do not start unrelated disabled workers.
4. Readiness: check private `opencode-review` health, `assured-review-worker` heartbeat,
   DB migration head, model routing and current registry status before creating a run.
5. Create a development-domain task assigned to the Arvento registry (`assurance_tier` remains
   `general-high-assurance`). Launch a **pinned** review request:

   ```json
   {
     "backend": "opencode-review",
     "source_branch": "feature/efficiency-report-speed-controls",
     "expected_commit": "14c40c9c5acbb8fd742fa2d13468c22e6badae8d"
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

### PRIMARY read-only preflight, 07.11.2026

- ChatGPT Pro subscription-linked API credits were verified in the owner's OpenAI Platform:
  **$110.96 remaining**, expiring **22.11.2026**; **Auto-reload OFF**. This confirms the
  organization's credit grant, **not** the project association of a server API key.
- Docker uses the exhausted default subnet pools. Empty existing networks belong to
  other Compose projects and are **not** suitable as spare staging networks.
- The host currently has no local IPv4 route in `172.23.0.0/16`; the optional fixed
  staging CIDRs require a fresh overlap check and explicit networking GO before creation.
- The protected `OPENAI_API_KEY` entry exists in PRIMARY's `.env.providers`, mode `0600`,
  but its project/rotation provenance is not verified. Because a key was previously
  pasted into chat, **do not use the existing key for inference until safely rotated and
  verified against the credited organization**.
- No new review container, Docker network, paid inference request, migration or
  production Arvento deployment was executed at this checkpoint.
