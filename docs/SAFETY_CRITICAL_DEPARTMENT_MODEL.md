# AI Orchestra — Safety-Critical Department Operating Model

## Purpose

This profile defines how AI Orchestra must operate when a project belongs to a safety-critical or highly regulated domain such as civil aviation, nuclear power, spaceflight, rail, medical devices, or comparable critical infrastructure.

The platform is not considered a certifying authority. AI agents are engineering tools/actors inside a controlled lifecycle. Final release/certification authority remains human and policy-bound unless a regulator-approved process explicitly allows otherwise.

## Reference operating patterns

The organizational model is inspired by publicly documented practices from:
- NASA software engineering, software assurance and independent verification & validation;
- FAA development assurance under DO-178C/DO-330 and associated guidance;
- Airbus design assurance and Safety/RAMS practices;
- U.S. NRC guidance for software requirements, V&V, reviews/audits, test documentation, configuration management and lifecycle controls;
- Westinghouse Common Q safety software lifecycle and qualified I&C platform practices;
- qualified development-tool practice such as Thales DO-330/TQL programs.

These references are used as patterns, not as a claim that AI Orchestra is certified to any of them.

## Three independent lines

### Line 1 — Engineering
Responsible for building the product.

Roles:
- Chief/Lead Engineer;
- System Architect;
- Requirements Engineer;
- Software Architect;
- Developer;
- Integration Engineer;
- DevOps/Build Engineer;
- Documentation Engineer.

Engineering may propose changes but cannot self-authorize release of safety-critical changes.

### Line 2 — Verification & Validation
Responsible for objective evidence that the implementation satisfies approved requirements.

Roles:
- Verification Lead;
- Test Architect;
- Independent Reviewer;
- Static Analysis Engineer;
- Integration/System Test Engineer;
- Requirements Traceability Auditor;
- Tool Qualification Engineer.

Independence rule:
- safety-critical code is not accepted solely by the model/agent that created it;
- a second prompt to the same agent is not independence;
- a second model may add diversity but does not replace organizational/process independence;
- V&V must be able to reject Engineering output and stop progression.

### Line 3 — Safety / Assurance / Release Authority
Responsible for deciding whether evidence is sufficient for the declared criticality and intended use.

Roles:
- Safety Assurance Lead;
- Software Assurance/Quality Lead;
- Cybersecurity Assurance Lead;
- Configuration Management Authority;
- Release/Certification Manager;
- Supplier Assurance Lead;
- Incident/CAPA Owner.

This line is fail-closed and cannot be overridden by Department Lead, Coder or ordinary Manager approval.

## Mandatory lifecycle

For critical projects the minimum lifecycle is:

1. classify project/system/software criticality;
2. define applicable standards and tailoring decisions;
3. establish approved system/software requirements baseline;
4. perform hazard/safety analysis and derive safety requirements;
5. create architecture and interfaces;
6. establish bidirectional traceability requirements -> design -> code -> tests -> evidence;
7. implement only against approved requirements;
8. independent review and static analysis;
9. unit/integration/system verification;
10. validation against intended use;
11. configuration audit and reproducible build;
12. safety/assurance review;
13. release decision by authorized human authority;
14. controlled deployment/installation;
15. operational monitoring, incident response and corrective action;
16. controlled maintenance and eventual retirement.

No safety-critical task is considered complete because code compiles or CI is green.

## Requirements and traceability

Every critical change must identify:
- source requirement(s);
- derived requirement(s), if any;
- hazard/safety requirement linkage where applicable;
- design element(s);
- implementation files/units;
- verification method and test cases;
- verification result;
- review findings and closure;
- released configuration item/baseline.

Orphan code and orphan requirements are release blockers unless explicitly justified and approved.

## Safety case / assurance case

For critical releases the result package must be evidence-oriented, not merely a narrative summary.

Minimum structure:
- claim: what safety/compliance property is asserted;
- argument: why available evidence supports the claim;
- evidence: requirements, analyses, reviews, test results, static-analysis results, configuration records, tool qualification records and build provenance;
- assumptions/limitations;
- unresolved risks/deviations;
- approvals and identities of accountable authorities.

## AI/tool qualification policy

AI models and orchestration components are treated as tools whose qualification burden depends on intended use.

### Lower qualification burden
AI may generate suggestions/code/tests when all outputs are independently verified by qualified downstream processes and no verification objective is eliminated because of AI output.

### Higher qualification burden
If AI output is used to:
- automatically satisfy a lifecycle objective;
- generate certification/safety evidence relied upon without independent verification;
- perform verification that removes or reduces another verification activity;
- make an autonomous release/safety decision;
then a formal tool qualification/validation strategy is required before such use is enabled.

Default policy: AI never signs its own evidence and never acts as release authority.

## Model diversity and common-cause failure

Using multiple agents backed by the same model/provider can create common-mode errors.

Critical workflows therefore distinguish:
- role independence;
- model diversity;
- provider diversity;
- deterministic/non-AI verification;
- human independent review.

For high criticality, QA must include at least one verification path that does not depend on the same generative model family that produced the change.

## Repository and configuration management

Critical repositories require:
- protected default branch;
- PR-only changes;
- immutable reviewed commit SHA;
- signed/attested build provenance where feasible;
- controlled baselines/tags/releases;
- exact dependency lockfiles;
- SBOM;
- reproducible build target;
- retention of source, tools, configs, logs and evidence needed to reproduce the release;
- no direct agent push/merge to protected branches.

## Execution isolation

Untrusted repository code is never executed in the Management Plane.

Install/build/test runs occur in ephemeral isolated runners with:
- no host Docker socket;
- no Control Plane/DB credentials;
- no unrelated repository access;
- restricted network egress;
- CPU/RAM/PID/time quotas;
- immutable base image identified by digest;
- ephemeral credentials with minimum scope;
- artifact/evidence export through a controlled channel.

## Change classes

### Class N — non-critical
Normal engineering workflow; automated AI review may be sufficient subject to project policy.

### Class C — controlled
Independent review and explicit manager approval required.

### Class S — safety/security significant
Requirements baseline, traceability, independent V&V, assurance gate, configuration audit and human release authority required.

### Class S+ — highest criticality
Adds diverse verification, stricter tool qualification, formal hazard/safety linkage, evidence retention, regulator/customer-specific certification plan, enhanced change control and mandatory human sign-off by independent authorities.

The system defaults upward when classification is ambiguous.

## Stop-the-line conditions

The workflow must stop automatically if any of the following occurs:
- missing/ambiguous safety classification;
- missing applicable standards profile;
- missing approved requirement for a safety-critical code change;
- traceability gap;
- failed/inconclusive verification;
- unavailable independent reviewer;
- tool/version mismatch from qualified baseline;
- dependency drift;
- build provenance mismatch;
- unresolved high/critical security finding;
- workspace or repository state ambiguity;
- model/provider identity drift during a controlled execution;
- missing approval bound to the exact commit/configuration;
- audit/evidence storage failure.

## Incident and CAPA model

A released defect triggers a controlled process:
1. preserve evidence and affected baseline;
2. classify severity and operational impact;
3. containment/rollback decision;
4. root-cause analysis;
5. identify process escape point;
6. corrective/preventive action (CAPA);
7. regression evidence;
8. independent approval;
9. lessons learned fed into organizational rules/tests.

AI may assist analysis but cannot close its own CAPA finding.

## Supplier and third-party assurance

For dependencies, external models, SaaS APIs and vendor software record:
- supplier identity;
- exact version/service/model;
- intended use;
- criticality;
- provenance/license;
- security posture;
- change/update policy;
- validation/qualification evidence;
- data confidentiality/retention policy;
- fallback/exit strategy.

Silent provider/model substitution is prohibited for controlled critical executions.

## Department readiness gates

Before claiming readiness for safety-critical projects, AI Orchestra must demonstrate:

### SC-G1 Process foundation
- standards/tailoring registry;
- criticality classification;
- requirements/traceability model;
- independent V&V workflow;
- assurance authority workflow;
- controlled configuration baselines.

### SC-G2 Platform assurance
- durable workflow execution;
- qualified/validated tool inventory;
- immutable audit/evidence records;
- isolated execution plane;
- reproducible/pinned build environment;
- disaster recovery and restore drills.

### SC-G3 Evidence automation
- requirement-to-test traceability;
- deterministic checks/static analysis;
- evidence package generation;
- model/provider provenance;
- change impact analysis;
- supplier/dependency records.

### SC-G4 Release authority
- exact-SHA approval;
- protected branches;
- independent configuration audit;
- safety/assurance sign-off;
- deployment rollback proof;
- no autonomous AI release authority.

### SC-G5 Qualification pilot
- run an end-to-end representative critical project in shadow/pilot mode;
- inject failures and configuration drift;
- perform independent audit of produced evidence;
- perform recovery/rollback exercise;
- document all deviations before production use.

## Bottom line

A full safety-critical department is not a chain of specialized chat agents. It is a controlled engineering organization in which requirements, independence, configuration, evidence, authority, qualification and lifecycle accountability are first-class system entities. AI is permitted to accelerate engineering work only inside those controls.