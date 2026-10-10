# G5 — Controlled Git & External Actions

Status: **G5.1 implementation in progress**. This increment builds the authorization/replay/reconciliation substrate only. It does **not** add a Git push/PR/merge/deploy executor and does not change the fail-closed capability guards.

## G5.1 objective

An external action must be represented by one immutable, content-addressed manifest before a human authorization can refer to it. Approval text alone is not execution authority.

The canonical action manifest binds:

- task identity when present;
- repository identity;
- action type (`git_push`, `pull_request`, `merge`, `deploy`, `external_write`);
- exact source SHA;
- exact desired/head SHA;
- destination;
- optional final Result Package digest;
- bounded JSON action payload.

The Control Plane canonicalizes the manifest and computes `action_digest = SHA-256(canonical JSON)`. The digest is globally unique in the current durable database. Re-submitting the same exact manifest is rejected rather than creating another authorization path.

## Authorization lifecycle

A G5 authorization is separate from the historical generic `approvals` table. Existing approvals retain their original management-record semantics and are never reinterpreted as external execution tokens.

G5 authorization records bind the exact `action_digest` and contain:

- requester identity from authenticated manager context;
- reason;
- expiry;
- decision identity/comment/time;
- one-time consumption identity/time;
- deterministic operation key.

Every decision and claim re-computes and verifies the controlled-action digest from durable fields. Database tampering or stale action content fails closed.

Expired authorizations cannot be approved or consumed. Capability denial is checked before consumption, so an otherwise valid authorization is not destroyed by a denied execution attempt.

## Deterministic replay identity

The operation key is generated server-side as:

`g5-<action_digest>`

Clients cannot choose it. Retries of the same exact action therefore retain the same idempotency identity. The authorization and effect ledger both persist this key, and uniqueness constraints reject duplicate claims inside the durable state.

This does not replace external reconciliation after database restore or an uncertain response. A future Git/external adapter must reconcile the real external state against the exact source/desired identity before repeating a side effect.

## Effect/reconciliation ledger

A successful claim creates a durable effect record in `reserved` state. G5.1 performs **no external side effect**.

The ledger can record reconciliation observations:

- `source_state`: observed state must equal the exact source SHA and records preflight reconciliation;
- `desired_state`: observed state must equal the exact desired/head SHA and closes the record as reconciled;
- `diverged` / `unknown`: the action becomes uncertain and requires operator/executor reconciliation.

The current API records supplied reconciliation evidence; G5.2 must add trusted provider/Git adapters that obtain these observations directly from the external system before any mutation.

## Capability boundary

Claiming an action remains impossible unless the existing Orchestra-owned guard is enabled:

- `deploy` requires `production_deploy_allowed=true`;
- all other G5.1 external actions require `external_write_allowed=true`.

G5.1 does not add an API that enables these guards. Production defaults remain `false`.

## High-assurance bridge from G4

For `general-high-assurance` and `regulated-critical` repositories, controlled action creation additionally requires:

- a bound Result Package digest;
- Result Package state `final`;
- Result Package `assurance.provenance_status == complete`;
- task/repository correlation with the action.

This prevents G5 authorization from bypassing the provenance controls established in G4.

## G5.1 acceptance gates

- canonical digest is stable across JSON key ordering;
- duplicate exact action is rejected;
- stale/tampered digest fails closed;
- authorization expiry is enforced;
- capability denial does not consume authorization;
- claim is atomic and one-time;
- deterministic operation key is server-owned;
- deploy uses the separate production-deploy capability;
- source/desired reconciliation requires exact immutable identities;
- high-assurance action requires complete final Result Package provenance;
- migration `20260917_0011 -> 20260925_0012` preserves legacy approvals unchanged;
- full API/schema/migration/runner/DR CI remains green.

## Out of scope for G5.1

- GitHub/GitLab/Bitbucket mutation adapters;
- autonomous push, PR creation, merge or deployment;
- trusted external-state observer/reconciler;
- branch-protection/ruleset mutation;
- release signing/SBOM publication;
- enabling external-write or production-deploy capability in production.
