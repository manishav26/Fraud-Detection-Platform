-- Cases and the audit log (week 3, step 5). Run by `make db-schema` as the database owner.
-- Safe to run again. Design: docs/alerts.md

-- alert-svc's own login: it may add and read, and change cases, but never change the audit log.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'alerts') THEN
    CREATE ROLE alerts LOGIN PASSWORD 'alerts_dev_only';   -- dev password only
  END IF;
  -- model-admin-svc's login (week 4, step 6): may only read and add audit rows
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'model_admin') THEN
    CREATE ROLE model_admin LOGIN PASSWORD 'model_admin_dev_only';   -- dev password only
  END IF;
END $$;

-- A flagged payment waiting for a person to decide. One per payment: txn_id is unique, so a
-- message delivered twice can't open two cases.
CREATE TABLE IF NOT EXISTS cases (
  case_id        bigserial PRIMARY KEY,
  txn_id         text NOT NULL UNIQUE,
  user_id        text NOT NULL,
  amount         double precision NOT NULL,
  payment_ts     timestamptz NOT NULL,
  score          double precision NOT NULL,
  decision       text NOT NULL CHECK (decision IN ('review', 'decline')),
  reasons        jsonb NOT NULL,
  model_version  text NOT NULL,
  degraded       boolean NOT NULL,
  opened_at      timestamptz NOT NULL DEFAULT now(),
  status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'fraud', 'not_fraud')),
  resolved_by    text,
  resolved_at    timestamptz,
  note           text
);
CREATE INDEX IF NOT EXISTS cases_open ON cases (opened_at) WHERE status = 'open';

-- Append-only record of who did what. Each row's hash covers its own fields and the previous
-- row's hash (audit/chain.py), so changing any row breaks the chain from there on.
CREATE TABLE IF NOT EXISTS audit_log (
  id         bigserial PRIMARY KEY,
  at         timestamptz NOT NULL,
  actor      text NOT NULL,
  action     text NOT NULL,
  target     text NOT NULL,
  details    jsonb NOT NULL,
  prev_hash  text NOT NULL,
  hash       text NOT NULL UNIQUE
);

-- Second layer: even a user who is allowed to update or delete is refused. Per statement, not
-- per row: a per-row trigger never fires when no row matches, so an empty table looked editable.
CREATE OR REPLACE FUNCTION audit_log_refuse_change() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only: % refused', TG_OP;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log;
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION audit_log_refuse_change();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;   -- merged into the one above

-- First layer: what alert-svc's login may do
REVOKE ALL ON cases, audit_log FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON cases TO alerts;
GRANT SELECT, INSERT ON audit_log TO alerts;
GRANT USAGE ON SEQUENCE cases_case_id_seq, audit_log_id_seq TO alerts;

-- What model-admin-svc's login may do: no cases at all, and the audit log only read and added to
GRANT SELECT, INSERT ON audit_log TO model_admin;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO model_admin;
