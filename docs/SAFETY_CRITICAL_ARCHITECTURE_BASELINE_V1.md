# AI Orchestra — Safety-Critical Architecture Baseline v1

## Purpose

This baseline defines the target architecture for a virtual engineering department capable of supporting high-value and eventually safety-critical development under controlled, project-specific assurance processes.

It is deliberately stricter than the current implementation. It is not a certification claim.

## Core operating principle

AI is allowed to propose, analyze and implement inside bounded capabilities. Authority is outside the model.

For safety-critical profiles the organization has three independent lines:

1. **Engineering** — requirements elaboration, architecture, implementation, integration.
2. **Independent Verification & Validation** — independently verifies requirements/design/code/tests/evidence using separate authority, methods and contexts.
3. **Safety / Assurance / Release Authority** — owns hazard/safety/compliance arguments and may stop release irrespective of Engineering status.

A second AI role or vendor may add diversity, but does not by itself establish organizational independence.

## Target planes

```text
Human Authority Domain
  release / certification / safety signatories
                 |
                 v
Management & Policy Plane
  Control Plane
  Identity / RBAC / MFA
  Policy Engine
  Approval Engine
  Configuration Baseline Authority
                 |
        +--------+---------+
        |                  |
        v                  v
Workflow Plane         Configuration Plane
  Temporal              Requirements Store
  schedules intent       Repo Manager
  no release keys        Model/Tool Registry
                         Project Assurance Profiles
        |                  |
        +--------+---------+
                 v
Execution Plane (UNTRUSTED)
  ephemeral isolated runners
  AI agents
  build/test workers
  cloned repositories
  project dependencies
  constrained egress
  NO baseline-write/release/signing credentials
                 |
                 v
Assurance & Evidence Plane
  independent V&V workflows
  deterministic verification tools
  traceability graph
  evidence ledger / WORM store
  artifact registry / SBOM / provenance
  CAPA / problem reporting
                 |
                 v
Controlled Release Transaction
  exact immutable digest set
  evidence completeness check
  independent approvals
  protected ref / artifact admission
```

## Authoritative object graph

No mutable label such as task name, branch name, PR number or workflow status is sufficient to identify a safety-relevant object.

Every controlled object is addressed by immutable identity:

- requirement baseline id + content hash;
- source tree / commit hash;
- build definition hash;
- dependency lock/SBOM digest;
- runner/builder image digest;
- model/tool Environment Baseline ID;
- test procedure/evidence digest;
- produced artifact digest;
- approval statement digest;
- release manifest digest.

PostgreSQL stores indexed control state and relationships. Git stores source history. Artifact/evidence storage stores immutable blobs/attestations. Temporal stores durable workflow history. None of those stores alone is the entire source of truth; release state is reconstructed from the signed/content-addressed object graph and reconciled control state.

## Environment Baseline ID (EBI)

A controlled AI/tool execution receives a unique immutable EBI computed from all behaviour-determining configuration, including as applicable:

- AI Orchestra application versions;
- workflow definitions/version;
- agent role/prompt versions;
- tool schemas and capability policy;
- model provider/model/version/fingerprint;
- decoding/runtime configuration visible to us;
- model router policy and fallback policy;
- RAG/index/corpus snapshot identities;
- runner image digest;
- toolchain/compiler/analyzer versions;
- dependency lock digests;
- project assurance policy version.

Any relevant change creates a new EBI. Existing evidence is never silently re-labelled to the new environment.

## Project Assurance Profile

Every registered project has a mandatory profile before controlled execution:

- domain: general / aviation / nuclear / space / medical / rail / other;
- criticality/assurance classification;
- data classification: public / internal / confidential / restricted;
- applicable standards/guidance registry;
- approved tailoring/deviations;
- AI intended uses by role;
- AI credit mode: none / controlled-assistance / explicitly-qualified scope;
- allowed providers/regions/models;
- repository/auth profile;
- execution profile;
- required verification independence;
- release authority/quorum;
- retention period;
- required evidence set;
- supplier/export/IP restrictions.

Unknown criticality or data classification is fail-closed.

## Requirements and traceability subsystem

Requirements are not free-form task descriptions. Controlled projects use a first-class requirements model with:

- approved baselines;
- parent/child decomposition;
- derived requirement declaration and rationale;
- safety/hazard links;
- design/code/test/evidence links;
- bidirectional completeness checks;
- orphan detection;
- semantic/content-hash invalidation after change;
- deterministic impact graph.

Coding/test agents have read-only access to approved requirement baselines unless a dedicated requirement-change workflow is explicitly authorized.

## Workflow semantics

Temporal (or equivalent durable workflow technology) provides durable orchestration, not exactly-once external effects and not release authority.

Every irreversible/external activity uses a common effect protocol:

1. Canonical input + input hash.
2. Idempotency key.
3. Current monotonic fencing generation.
4. Policy/approval check over exact immutable inputs.
5. Prepare intent recorded transactionally.
6. External effect requested by a minimal-privilege trusted service.
7. Result identity/digest verified.
8. Commit/admission record written.
9. Evidence written before workflow progresses.
10. Reconciliation can safely determine completed/not-completed after crash.

Retries, duplicate delivery and zombie workers are assumed normal.

## Repository Manager

Repo Manager, not AI/OpenCode, owns Git credentials and protected Git effects.

Responsibilities:

- validate/register remote;
- fetch immutable refs;
- create task worktrees/proposal refs;
- compute status/diff/tree identities;
- clean task workspaces;
- prepare commit objects;
- push proposal branch after scoped approval;
- never directly push/merge protected default branch;
- preserve audit/provenance.

AI sees worktree contents but not Git credentials.

## Execution Plane

All repository/build/test execution is treated as hostile.

Mandatory properties for controlled profiles:

- disposable per-task/per-campaign sandbox;
- no Docker socket;
- no host `/root`, `.env` or unrelated repositories;
- no database/control-plane network path;
- CPU/RAM/PID/time limits;
- constrained or denied network egress by phase;
- read-only base image + explicit writable workspace;
- no baseline/release/signing credentials;
- brokered capability tools instead of unrestricted privileged shell;
- dependency install/build/test scripts execute only in the execution domain;
- runner output is a proposal until trusted admission verifies digest/provenance.

## AI / Model Registry

Every model/tool entry records:

- provider;
- model identity/version/fingerprint as available;
- deployment type (hosted/self-hosted);
- data classification eligibility;
- intended role/use;
- credit/no-credit classification;
- qualification/validation evidence reference where applicable;
- allowed fallback policy;
- known limitations;
- evaluation suite version;
- last qualification/validation date;
- incident history.

For safety-controlled executions, silent provider/model substitution is prohibited. Failure to serve an approved model suspends or restarts the affected evidence path according to policy.

## Verification independence

Independent V&V has separate:

- role ownership;
- identities and permissions;
- verification policy/configuration baseline;
- work queue and evidence namespace;
- ability to reject Engineering output;
- context assembly rules.

Safety-critical verification does not receive developer rationale by default if that would bias independent interpretation. The approved requirement baseline, artifact under review and verification criteria are sufficient inputs unless a controlled investigation requires more.

AI reviewer output is supplemental unless the project has an accepted basis to take credit from that function.

## Test provenance

Every controlled test records provenance:

- source requirement(s);
- test design/review identity;
- oracle derivation;
- input artifacts visible to the test author/tool;
- environment/tool identity;
- test procedure digest;
- executed artifact digest;
- runner digest;
- raw result/evidence digest.

Requirements-based tests and structural/coverage-supplement tests are different classes. Tests created from source code solely to hit uncovered branches cannot silently become requirements-based evidence.

Mutation/fault-injection is used to check that tests can actually detect requirement violations; passing an implementation is not by itself evidence of oracle correctness.

## Evidence Plane

Certification/assurance evidence is distinct from ordinary application logging.

Controlled evidence records at minimum:

- object identities/digests;
- EBI;
- canonical external AI request envelope where applicable;
- model identity returned/observed;
- retrieved source identifiers/hashes;
- tool calls and results used for decisions;
- truncation/error/retry/fallback events;
- visible AI outputs used downstream;
- human findings/decision and exact reviewed object digest;
- deterministic test/analyzer output;
- approval signatures/attestations;
- provenance and timestamps.

Hidden chain-of-thought is not required or assumed available.

Evidence is append-only/tamper-evident, retained according to project profile, with completeness/gap detection and restore/reconstruction drills.

## Human authority

For safety-critical profiles the following remain human-authorized unless a project-specific accepted assurance basis explicitly says otherwise:

- approval of safety/derived requirements;
- acceptance of unresolved safety deviations;
- acceptance of V&V findings/dispositions;
- release/certification declaration;
- production deployment/activation of a safety baseline;
- break-glass use;
- changes to criticality/standards/tailoring;
- qualification/validation acceptance for AI/tool intended use.

Critical release approval is bound to exact source/artifact/evidence digests and is non-delegable to an agent token.

## Supply-chain controls

Controlled build/release path includes:

- dependency lock;
- internal/digest-pinned dependency source for restricted profiles;
- SBOM;
- vulnerability and licence checks;
- provenance attestation;
- builder identity/digest;
- source provenance/protected review controls;
- supplier/provider inventory and change notification process where contractually available;
- incident/CAPA linkage.

SLSA is used as a supply-chain assurance pattern, not as a safety certification substitute.

## Release transaction

A release candidate is an immutable manifest containing exact:

- requirements baseline;
- source revision;
- configuration data;
- toolchain/build definition;
- artifact(s);
- SBOM/provenance;
- test/verification evidence;
- unresolved deviations;
- assurance verdicts;
- human approval signatures;
- rollback/recovery reference.

Release admission recomputes and verifies the manifest. A change to any bound input invalidates the approval/evidence according to the impact graph.

## Break-glass

Break-glass is not a hidden bypass.

For critical profiles it requires:

- dual human authorization;
- explicit reason/scope/expiry;
- dedicated audit event;
- elevated monitoring;
- no agent capability escalation beyond declared scope;
- automatic marking of affected evidence as requiring re-verification;
- post-event review/CAPA.

## Failure-injection qualification of the factory

Before advancing maturity gates, run repeatable tests for:

- worker crash before/after external effect;
- duplicate activity/webhook delivery;
- zombie worker after timeout;
- network partitions;
- stale database restore;
- corrupted/missing evidence write;
- changed model/provider identity;
- model malformed/truncated response;
- prompt-injected repository/RAG/tool output;
- malicious dependency/install script;
- compromised runner producing wrong digest;
- expired/replayed approval;
- wrong-tenant/wrong-project operator action;
- clock skew/time rollback;
- retry storm/resource exhaustion;
- failure of backup/restore/reconciliation;
- seeded safety defect and review escape;
- build/rebuild divergence.

A documented control is not considered closed until its negative/failure test passes.

## Organizational pattern references

The design follows public high-assurance patterns rather than copying any proprietary company process:

- NASA separates software engineering from software assurance/software safety/IV&V and explicitly describes technical, managerial and financial IV&V independence.
- Airbus Protect publicly describes Design Assurance, audits/gap analysis, lifecycle compliance evidence and Safety/RAMS work such as FHA/PSSA/SSA/FMEA/CCA.
- Westinghouse publicly documents Common Q software design around requirements specifications, configuration management, test documentation, V&V/reviews/audits and lifecycle processes; its modern safety platform material explicitly emphasizes determinism, diversity and long-term lifecycle support.
- FAA AC 20-115D recognizes DO-178C and DO-330 as accepted assurance/tool-qualification references; EASA is actively developing AI trustworthiness guidance, so AI assurance must be treated as evolving and project-specific.

## Near-term engineering rule

Do not optimize for autonomous throughput before identity, configuration, evidence and independent assurance are trustworthy.

The next engineering target is **G1 Durable Core**, not Repository Manager autonomy.
