-- P7 operations layer (SPEC §14.2 / §17.1).
--
-- runs.cancel: the cancel REQUEST is durable so a control-plane caller can
-- distinguish "cancel requested" from "cancel effected" (提交 ACK、取消请求
-- 都不等于成功完成). The coordinator refuses to claim runs carrying the
-- flag; a queued run is resolved to suppressed at request time, a running
-- run keeps its flag until the in-flight worker observes it or its lease
-- is swept on restart.
ALTER TABLE runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN cancel_requested_ms INTEGER;
