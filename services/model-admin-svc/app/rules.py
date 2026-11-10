"""When a signed promotion may be accepted (week 4, step 6). Design: docs/mlflow.md

Pure functions over the audit log's model rows (newest first), so they are easy to test. The
signature itself is checked before these run (registry/signing.py).
"""

from registry.signing import MODEL_NAME

# Audit-log actions for model changes. A failed publish gets its own row, so the log never claims a
# change that didn't go out, and never hides an attempt that did.
ACTIONS = {"promote": "model_promoted", "rollback": "model_rolled_back"}
FAILED = "model_promotion_failed"
MODEL_ACTIONS = (*ACTIONS.values(), FAILED)


def live_changes(rows):
    """Promotions and rollbacks that went out, newest first (failed publishes left out)."""
    failed = {r["details"].get("sequence") for r in rows if r["action"] == FAILED}
    return [r for r in rows if r["action"] in ACTIONS.values()
            and r["details"].get("sequence") not in failed]


def check(route, claims, rows):
    """None if `claims` may go out through `route` ("promote" or "rollback"), else the reason."""
    action = claims.get("action")
    if action != route:
        return f"this message is for {action!r}, not {route!r}"
    if claims["model"] != MODEL_NAME:
        return f"this message is for model {claims['model']!r}, not {MODEL_NAME!r}"

    # Every recorded attempt counts, failed ones too: a token is used once, even if its publish
    # failed. Retrying means signing again, which gives a new, higher sequence.
    sequences = [r["details"].get("sequence") for r in rows if r["details"].get("sequence")]
    if sequences and claims["sequence"] <= max(sequences):
        return (f"replayed or old message: sequence {claims['sequence']} is not newer than "
                f"{max(sequences)}")

    reason = (claims.get("reason") or "").strip()
    if route == "promote":
        gate = claims.get("gate")
        if not isinstance(gate, dict) or "passed" not in gate:
            return "the promotion gate's result is missing"
        if not gate["passed"] and not reason:
            return "the promotion gate said keep the current model: an override needs a reason"
        return None

    # A rollback goes back exactly one step: to the version that was live before the current one
    if not reason:
        return "a rollback needs a reason"
    live = live_changes(rows)
    if len(live) < 2:
        return "nothing to roll back to: fewer than two model changes are on record"
    previous = live[1]["details"]["version"]
    if str(claims["version"]) != str(previous):
        return f"a rollback must go back to version {previous}, the one live before this"
    return None


def details(claims):
    """What the audit row records: everything the key holder signed, minus the JWT fields."""
    return {"version": str(claims["version"]), "label": claims.get("label"),
            "sha256": claims["sha256"], "sequence": claims["sequence"],
            "reason": (claims.get("reason") or "").strip(), "gate": claims.get("gate")}
