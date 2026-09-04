# AI Orchestra — Independent Design Review 2026-09-04

## Status

This document consolidates three independent hostile reviews of AI Orchestra:

- Aviation / certification review — Claude Opus 5;
- Nuclear digital I&C assurance review — Gemini 3.8 Flash;
- Distributed systems / security red-team review — Grok 4.6.

The reviews are inputs, not authority decisions. Their claims were independently checked against public FAA, EASA, NASA, NRC, IEC, NIST, SLSA and Temporal material before being accepted into the architecture baseline.

Important source limitation: the exported Gemini nuclear review contains all 15 requested scenarios, but is truncated at the start of `PART II: MANDATORY ORGANIZATIONAL ROLES`. Its missing roles/gates/evidence-checklist section must be re-run before the nuclear-specific review is considered complete.

## Decision

Current maturity remains **G0 / Lab**. AI Orchestra is not approved for autonomous work on safety-critical or confidential high-value product baselines.

The platform may progress only through explicit maturity gates. Safety-critical use begins in shadow/no-credit mode and remains program-specific; no generic claim of DO-178C, DO-330, IEC 61513/60880, NRC, NASA or other certification is made.

## Reviewer agreement

Legend: A = Aviation, N = Nuclear, S = Security/Distributed Systems.

| ID | Consolidated finding | Reviewers | Severity | Required direction |
|---|---|---|---|---|
| SCF-001 | Certification/regulatory profile is not a first-class project object | A,N | Critical | Project criticality, applicable standards, tailoring, authority status and AI intended use must be baselined before controlled work |
| SCF-002 | AI Orchestra's own behaviour is not fully configuration-controlled | A,N,S | Critical | Environment Baseline ID (EBI) over prompts, policies, model identity, workflow definitions, runner images, tool schemas and retrieval indexes |
| SCF-003 | Engineering, V&V and Safety/Assurance are not technically and organisationally independent | A,N | Catastrophic | Separate authority domains, identities, baselines, budgets/ownership where applicable; Assurance can stop release |
| SCF-004 | Multiple AI agents can fail by common cause | A,N | Catastrophic | Model diversity is supplemental only; every safety-significant verification chain needs a causally dissimilar human or deterministic verification path |
| SCF-005 | Requirements/derived requirements/traceability are not first-class controlled data | A,N | Catastrophic | Baselined requirements store, derived-requirement workflow, bidirectional traceability, change-impact invalidation |
| SCF-006 | AI-generated tests can become tautological or coverage-driven | A,N | Catastrophic | Requirements-based test provenance, independent oracle derivation, mutation/fault-injection checks, structural coverage cannot be gamed by source-derived tests |
| SCF-007 | Human verification throughput can be overwhelmed by AI output | A,N | Critical | Back-pressure based on verification capacity; seeded-defect calibration; no batch rubber-stamping |
| SCF-008 | AI intended use and tool-credit boundary are undefined | A | Critical | Tool/AI registry with intended use, credit/no-credit status, validation/qualification strategy and authority coordination where required |
| SCF-009 | Repository content is an instruction-injection and code-execution threat | A,N,S | Critical | Treat repo/RAG/tool output as untrusted data; broker capabilities; disposable isolated runners; deny secrets and baseline-write capability |
| SCF-010 | Model/provider identity can drift or silently fall back | A,N,S | Critical | Controlled model registry; exact served identity recorded; controlled-mode fallback prohibited unless explicitly approved and re-verified |
| SCF-011 | Artifact identity is not strong enough if based on mutable workflow state | A,S | Critical | Content-addressed artifacts and signed provenance; database rows index identity rather than define it |
| SCF-012 | Durable workflow does not make external effects exactly-once | S | Critical | Idempotency keys, fencing generations, transactional intent/outbox and admission uniqueness for Git/registry/release effects |
| SCF-013 | Temporal/workflow history must not be the policy authority or certification evidence | S | Critical | Workflow engine schedules durable intent only; policy, approval and evidence authority live outside worker state |
| SCF-014 | Evidence can be incomplete, mutable or unreconstructible | A,N,S | Critical | Append-only/WORM evidence, cryptographic binding, gap detection, provenance and reconstruction drills |
| SCF-015 | Human approval can be separated from the exact released content | A,N,S | Critical | Approval binds exact artifact/source/evidence digests, scope and expiration; non-replayable release authorization |
| SCF-016 | Builds are not yet hermetic/reproducible enough | A,N,S | Critical | Locked dependencies, digest-pinned builders, SBOM, provenance, no network during controlled build; reproducibility/equivalence policy |
| SCF-017 | Runner compromise can forge output if runners hold publish credentials | A,N,S | Critical | Runners are hostile; no release/signing keys; upload admitted only after digest/provenance verification by trusted control plane |
| SCF-018 | Restore/split-brain can resurrect stale authority | S | Critical | Monotonic epoch/fencing that cannot rewind with DB restore; cross-store reconciliation and freeze on disagreement |
| SCF-019 | Supplier and dependency assurance is incomplete | A,N,S | Critical | Provider/dependency/tool inventory, provenance, vulnerability/license/export checks, supplier-change and incident process |
| SCF-020 | Change impact and evidence invalidation are insufficiently deterministic | A,N | Critical | Deterministic dependency/trace impact graph; any changed input invalidates affected evidence until re-established |
| SCF-021 | Systemic AI/tool defects need CAPA and fleet-wide impact sweep | A,N | Critical | Tool/process-induced defect taxonomy; EBI-scoped search across projects/releases; quarantine implicated environment |
| SCF-022 | Long-term archive and continued-engineering capability are not proven | A,N,S | Critical | Archive source, requirements, tools, model metadata, evidence and builders; periodic recovery/reconstruction drills |
| SCF-023 | Release authority cannot be an AI role | A,N,S | Catastrophic | Named human release/safety authority for safety-critical profile; AI output remains proposal/evidence input, never authority |
| SCF-024 | Identity/secrets boundaries are not yet high-assurance | A,N,S | Critical | Strong workload identity, least privilege, short-lived credentials, separate signing trust, MFA/hardware-backed authority for critical release actions |
| SCF-025 | The factory itself lacks systematic failure-injection qualification | A,N,S | Critical | Continuous chaos/red-team suite: crash, duplicate, partition, restore, provider drift, prompt injection, hostile runner, evidence gap |
| SCF-026 | Compliance/certification claims must not be generated as unverifiable prose | A,N | Critical | Compliance status derived deterministically from evidence/objective ledger; AI may draft narrative but cannot mint claims |
| SCF-027 | Human competency can erode under automation | A,N | Major | Periodic AI-free competency exercises and retained unaided emergency capability |
| SCF-028 | Data classification/export/IP/tenant isolation are insufficiently explicit | A,N,S | Critical | Project data classification controls provider/region, storage, RAG/cache and repository access; fail closed on unknown classification |
| SCF-029 | Time is a security input for approvals/evidence | A,S | Major | Trusted timestamping for critical attestations; freeze on large clock anomalies; expiration not based solely on worker clock |
| SCF-030 | Break-glass can silently destroy assurance | A,S | Critical | Dual control, time-bound break-glass, explicit audit and automatic invalidation/re-verification of evidence obtained under bypass |

## Unanimous blockers / invariants

The following are treated as architecture invariants because they recur across reviewers and are supported by established high-assurance engineering practice:

1. AI agents are untrusted principals, not authority holders.
2. Engineering cannot self-verify through another prompt or another role using the same authority domain.
3. Safety/Assurance must be able to reject and stop Engineering output.
4. Requirements, source, tests, build outputs, approvals and evidence are content/version bound.
5. A release is impossible if exact producing inputs/environment/evidence cannot be reconstructed.
6. Every external side effect is idempotent and fenced; duplicate workflow delivery is normal.
7. Model/provider drift is a configuration change, not an operational detail.
8. Repository/build content is untrusted code/data and executes only in an isolated execution plane.
9. Approval is for an exact immutable object set, not a task/PR name.
10. Ambiguous or inconsistent state fails closed.

## Reviewer claims NOT accepted as standards facts

Hostile reviews intentionally overstate. The following are retained as conservative design options or open certification questions, not represented as literal regulatory requirements:

1. **"LLMs can never be qualified at any TQL."** Not accepted as a standards fact. Intended use drives qualification/validation obligations; regulators are actively developing AI assurance approaches. Our policy is to take no certification credit from a generative model unless an accepted project-specific assurance/qualification basis exists.
2. **"Self-hosted models are always mandatory."** Not universal. For restricted/safety-controlled work, immutable model identity and contractual/data controls are mandatory; self-hosting is the preferred default when a hosted provider cannot meet them.
3. **"10^6 repeatability trials are required."** Reviewer-suggested stress test, not a cited requirement. We will define statistically justified qualification tests per intended use.
4. **"Two bit-identical rebuilds are required by DO-178C."** Not treated as a literal DO-178C objective. We adopt reproducible/hermetic build evidence as an internal high-assurance control; exact bit reproducibility is a target where technically feasible, otherwise controlled equivalence must be demonstrated.
5. **"Dwell time/scroll telemetry proves human review."** Not reliable enough for a hard gate. It may be secondary evidence; seeded-defect detection, signed review scope and independent findings are stronger.
6. **"Full model chain-of-thought must be archived."** Rejected. We archive the canonical external request envelope, model/config identity, retrieved source hashes, tool calls, visible outputs and decisions. Hidden provider reasoning is neither required nor assumed available.
7. **"Agents must have no shell or HTTP at all."** Too absolute. Safety-controlled runners use capability-brokered, allowlisted tools and constrained egress; unrestricted general-purpose shell/network access is prohibited.
8. **"AI may not augment human assurance roles."** Too absolute. AI may assist those humans, but may not own independence, sign compliance, or replace the required authority without a separately accepted assurance basis.

## Updated maturity gates

### General platform

- **G0 Lab** — current state; public/test repositories only; no high-value private safety baseline.
- **G1 Durable Core** — migrations, locked dependencies/builds, backup+restore drill, durable workflow PoC, idempotency/fencing foundation.
- **G2 Identity & Configuration Baseline** — EBI, strong identity, policy-as-code, model/tool registry, data classification.
- **G3 Repository & Sandbox Safety** — Repo Manager, content-addressed workspaces, isolated hostile runners, supply-chain controls.
- **G4 Evidence & Independent Assurance** — requirements/traceability, independent V&V domain, WORM/provenance/evidence graph, deterministic evidence invalidation.
- **G5 Controlled Git/Release** — exact-digest approvals, protected refs, signed provenance, reconciled release transaction, rollback.
- **G6 High-Assurance Operations** — DR, chaos qualification, CAPA, long-term archive drills, supplier management, continuous factory red-team.

### Safety-critical overlay

- **SC0 — prohibited**: no safety-critical project data/work.
- **SC1 — shadow/no-credit**: AI output quarantined; conventional process is complete without AI; no AI verdict closes assurance objectives.
- **SC2 — controlled assistance**: AI-generated engineering artifacts may enter controlled workflow only after complete independent verification per project plan; human/deterministic assurance owns acceptance.
- **SC3 — limited credited automation**: only functions with an accepted intended-use qualification/validation basis may receive process credit; exact scope is project/regulator specific.
- **SC4 — authority-coordinated production**: project-specific approved means of compliance/licensing basis, validated factory configuration, qualified personnel and release authority. This is never a generic platform certification claim.

## Immediate implementation order

P0 before Repository Manager autonomy:

1. Alembic baseline and schema-version preflight.
2. Dependency locking and reproducible CI semantics.
3. Backup restore drill and recovery runbook.
4. Temporal durable-workflow PoC plus application-level idempotency/fencing design.
5. EBI schema and immutable configuration identity.
6. Content-addressed artifact/evidence object model.
7. Project classification + assurance profile.
8. Strong approval object bound to exact digests.
9. OpenCode toolchain/sandbox hardening.
10. Independent assurance/evidence architecture design review.

## Nuclear review completion

The Gemini export is incomplete after its 15 scenarios. Re-run only the missing portion before closing the independent review:

> Continue the previous nuclear AI Orchestra review from `PART II: MANDATORY ORGANIZATIONAL ROLES`. Do not repeat the 15 failure scenarios. Complete: (1) mandatory organizational roles; (2) mandatory technical segregation; (3) mandatory lifecycle gates; (4) forbidden autonomous AI actions; (5) minimum evidence package for each safety-related release; (6) readiness checklist before a nuclear safety pilot. Clearly distinguish regulatory/public-guidance-backed requirements from your conservative reviewer recommendations. Do not invent clause numbers. Keep every recommendation testable and identify which controls are hard gates.

## Closure rule

A finding is not closed because a document says it is fixed. Closure requires executable evidence: tests, policy enforcement, failure-injection result, recovery drill, cryptographic verification or independent review as applicable.
