# Codex ChatGPT subscription lane: operator slice for issue #49

## Scope and state

This is an operator-managed host lane, not a Control Plane execution worker.
`scripts/codex_task.py` creates a new Git worktree at an exact base commit and
invokes Codex CLI using the operator's ChatGPT login. It has no Model Gateway,
OpenCode, AITUNNEL, provider API key, Control Plane DB credential, Docker socket,
deployment credential, or automatic paid fallback. No production rollout is part
of this change. The default Control Plane development execution path remains the
existing one until the critical integration items below are implemented and
reviewed. Do not claim this slice completes `execution_runs` or G4 Result Package.

The OS account is the security boundary for the writable role. Run only as the
unprivileged `codex-worker`; keep its `HOME` and `CODEX_HOME` separate from every
other service account. Codex receives an allowlisted environment. The operator
must not give the account privileged groups, production mounts, secrets, or a
production worktree. `--role developer --allow-write` explicitly permits edits
only in the new task worktree; architect, QA and reviewer run with Codex
`read-only` sandbox and a post-run workspace mutation check. The CLI may execute
commands inside its sandbox; do not run it as root or grant this account admin
rights. Human review remains required before any commit, push, PR, or deploy.

## Reproducible operator command

From a trusted checkout, prepare a scoped prompt file outside source control.
Confirm the repository's exact base SHA and that it is clean. Use a unique task
ID and output directory; do not point to the G2 managed workspace volume.

```bash
sudo -u codex-worker -H env CODEX_HOME=/var/lib/ai-orchestra/codex-worker/.codex \
  python3 scripts/codex_task.py \
  --repository /path/to/trusted/repository \
  --base <reviewed-exact-commit-sha> \
  --task-id issue49-001 \
  --role developer --allow-write \
  --prompt-file /path/to/operator-scoped-prompt.txt \
  --worktrees /var/lib/ai-orchestra/codex-worker/worktrees \
  --output /var/lib/ai-orchestra/codex-worker/results \
  --timeout 600
```

Use `--role architect`, `qa`, or `reviewer` without `--allow-write` for read-only
work. Default models are pinned in the script by role; an explicit `--model`
override is recorded in `run.json`. A missing or limited model ends in a failed
run; the operator must review it before starting a new task ID. No retry or paid
provider substitution occurs. The installed `orchestra-task` wrapper supplies
its own model, bounds and subscription guard; this repository does not change
that installed wrapper.

Inspect `results/<task-id>/run.json`, `events.jsonl`, `final.txt`, and
`stderr.log`. `run.json` records the base commit/tree, prompt SHA-256, final
workspace HEAD/status, event/final/stderr hashes, outcome, exit code, timestamps,
and paths. An initial `running` checkpoint is written before inference; a stale
checkpoint means unknown outcome, never success. `final_received` plus a
`turn.completed` event and zero process exit are required for `completed`.
Read-only workspace mutation forces failure. `usage` and `api_cost` are
`unknown`; do not record subscription usage as zero API cost. Logs are local
operator evidence, not yet durable CP audit or G4 evidence objects. Preserve
them for manual review; their contents are untrusted agent output.

## Rollout and rollback

This slice requires no migration, feature flag, service restart, or production
configuration change. Test it on synthetic repositories first. To stop using
the lane, stop submitting new operator commands and retain all result directories
and worktrees for audit. Do not automatically replay old failed executions.
An interrupted run with `outcome=running` needs manual inspection; reusing its
task ID is rejected. Reversing this code change does not alter CP rows.

## Critical integration TODO before making this the Control Plane default

1. Add an explicit `codex` lane and durable request/lease/fencing generation in
   `execution_runs` (or a related table), with a reviewed additive migration and
   rollback. Exclude Codex runs from the OpenCode worker query. Preserve old
   failed runs without replay. Set the default only after staging acceptance.
2. Bind each request server-side to the verified G2 repository/workspace identity,
   preflight digest and exact commit. Recheck repository trust and workspace
   readiness immediately before the host side effect. A separate host checkout
   needs an independently verified source tree; never mount the G2 managed
   volume or Control Plane credentials into the Codex account.
3. Design a narrow, authenticated, fail-closed result transport between CP and
   the host worker. The host must not receive manager Basic auth, DB passwords,
   `auth.json`, API keys, or production credentials. A human-mediated export and
   import is acceptable for initial acceptance, but must have single-use
   admission and exact request/evidence hashes. Reject duplicate/stale lease
   generations and partial results. Do not expose arbitrary shell, environment,
   path, or model-provider fields to task authors.
4. Record terminal outcome, bounded evidence and actual usage status in CP;
   validate `events.jsonl`, `final.txt`, `run.json`, exact commit/tree and
   workspace status before marking execution complete. Keep subscription cost
   unknown unless an authoritative accounting source supplies it. Retain
   approval, capability guard, G2 preflight/fencing and G3 runner boundaries.
5. Exercise crash recovery, duplicate claims, trust revocation, role constraints,
   auth isolation, model limit failure and rollback in staging. Do not call the
   operator evidence a CP Result Package before these checks pass.

The main residual risks in this slice are that local evidence is mutable by the
operator account, the prompt is operator supplied rather than CP scoped, and
`execution_runs` does not track these jobs. It is therefore unsuitable as an
automatic Control Plane default today.
