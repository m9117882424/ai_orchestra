# Codex subscription container confinement (Issue #49)

These security profiles are required for an **opt-in production** subscriber. Do
not use `apparmor=unconfined` or `seccomp=unconfined`.

- `ai-orchestra-codex-worker.apparmor`: enforcing per-worker profile. Allows
  nested bubblewrap mounts and user namespaces, but still mediates the
  process with AppArmor and explicit denies for host Docker sockets,
  /proc/sys and /sys writes. It is **not** a substitute for Docker's mandatory
  read-only rootfs, G2 read-only mount, no-new-privileges, cap_drop=ALL,
  UID 10001 and isolated network. The policy deliberately allows mounts
  inside an unprivileged user namespace; kernel namespace privilege checks
  remain in force. Review any subsequent changes to the profile.
- `codex-bwrap-seccomp.json`: Docker default-deny syscall profile with
  only clone(CLONE_NEWUSER), unshare, mount, umount2 and pivot_root
  extended to let the nested Codex bubblewrap sandbox run. Other default
  restrictions remain.

Operations, on the authorized host:

1. Install `ai-orchestra-codex-worker.apparmor` in
   `/etc/apparmor.d/ai-orchestra-codex-worker`, mode 0644. Parse with
   `apparmor_parser -Q`, then load via `apparmor_parser -r`. Verify
   `aa-status` says the profile is enforcing.
2. Set `CODEX_SANDBOX_SECCOMP_PROFILE` to the absolute path of the
   **reviewed** seccomp JSON file. The opt-in Docker Compose overlay
   fails closed without this variable.
3. Confirm the container's effective AppArmor profile using
   `cat /proc/self/attr/current`; check `CapEff=0`,
   `NoNewPrivs=1`, `Seccomp=2`, non-root UID 10001, RO root/G2.
4. On a **test** repository, execute
   `codex sandbox -- git -C /workspace/worktrees/managed/<UUID> rev-parse HEAD`
   with network disabled; verify it returns the immutable commit. Verify
   G2 is not writable and the Docker socket is absent.
5. Verify stopped worker and token rotation; never start this in the same
   task as an unreviewed database migration. Preserve audit/evidence on
   rollback and do not auto-restart failed model runs.

The profile's mount policy is broader than a fully per-path, per-filesystem
AppArmor policy. A dedicated VPS/container engine provides stronger
isolation for untrusted repositories. Production may process only
approved read-only repositories until that boundary is strengthened.
