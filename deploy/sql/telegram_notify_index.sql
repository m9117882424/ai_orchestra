-- G5 Telegram notifier: apply once, separately from transactional migrations.
-- Do not run CREATE INDEX CONCURRENTLY inside BEGIN/COMMIT.
-- This is an explicit DBA prerequisite before enabling the notifier.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ai_orchestra_audit_events_created_id
    ON public.audit_events (created_at, id);

-- If a conflicting index of this name exists with another definition, the
-- notifier readiness check will refuse to start until the DBA fixes it.
