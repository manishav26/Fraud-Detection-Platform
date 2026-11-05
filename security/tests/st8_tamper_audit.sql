-- ST8 attack: someone with database owner rights edits one audit row by hand, getting past the
-- append-only trigger by switching it off. make verify-audit must name that row.
-- make st8-tamper ROW=3     (and make st8-restore ROW=3 to put it back)
-- ROW is a position (the 3rd row), not an id: ids have gaps, because Postgres never reuses one,
-- even from a transaction that was undone (as the tests' are).
-- One transaction: if anything fails, the trigger is never left switched off.
BEGIN;
ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only;
UPDATE audit_log SET actor = :'actor'
  WHERE id = (SELECT id FROM audit_log ORDER BY id OFFSET :row - 1 LIMIT 1)
  RETURNING id, actor, action, target;
\set changed :ROW_COUNT
ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only;
-- Stop with an error (and undo everything) if no row was changed: a silent miss proves nothing
SELECT CASE WHEN :changed = 1 THEN 'changed 1 row'
            ELSE ('no audit row at position ' || :row)::int::text END AS result;
COMMIT;
