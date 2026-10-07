-- Migration 009: the authorization context of a user-originated wake
-- (SPEC §22.1 item 6).
--
-- A wake that is not attached to a job reaches the policy layer with no
-- grant scope, so every proposal it produces is suppressed with
-- `grant_missing` — the "user said something, so do something about it"
-- path is dead without this.
--
-- The column is written by exactly one method, `Store.admit_user_wake`,
-- which the facade calls from its trusted entry point. `Store.admit_event`
-- (the generic path used by hooks and manual triggers) never writes it, so
-- a caller cannot mint authorization by calling the ordinary entry point:
-- NULL means "no authorization context" and stays that way.
--
-- The value is a validated envelope:
--   {"grant_refs": [...], "destination_ref": "...", "delivery_policy": {...}}
-- It binds grants that already exist; it cannot widen a grant's scope.
ALTER TABLE events ADD COLUMN authz_json TEXT
 CHECK(authz_json IS NULL OR json_valid(authz_json));
