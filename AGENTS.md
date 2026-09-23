# Data migrations

When adding a data point or replacing existing values with a new master, identify every existing row and report that depends on it. Include an idempotent backfill in the same change and verify the before/after counts. If historical values cannot be derived reliably, ask the user for the source or mapping before treating the feature as complete. Do not silently replace missing history with zero or invented values. Preview changes before applying them to a database; follow `docs/healthos-agent-rules.md` for database safety.
