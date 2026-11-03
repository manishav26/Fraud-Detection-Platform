"""Check the whole audit log. Exit code 1 and the first broken row if it was tampered with (ST8).

    make verify-audit          (compose)        make k8s-verify-audit   (cluster, through a tunnel)

What it can't see: rows removed from the very end, or a chain someone rebuilt in full with
database owner rights. Anchoring the latest hash outside the database would catch both (later).
"""

import os
import sys

import psycopg

from audit.chain import first_broken

DSN = os.getenv("AUDIT_DSN", "postgresql://fraud:fraud_dev_only@localhost:5432/fraud")


def main():
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, at, actor, action, target, details, prev_hash, hash"
                    " FROM audit_log ORDER BY id")
        rows = cur.fetchall()
    broken = first_broken(rows)
    if broken:
        row_id, reason = broken
        sys.exit(f"BROKEN at row {row_id}: {reason}. Checked {len(rows):,} rows.")
    head = rows[-1][7][:16] if rows else "-"
    print(f"OK: {len(rows):,} rows, chain intact. Latest hash starts {head}")


if __name__ == "__main__":
    main()
