"""D3, probe detection per card (E6, threat 14). Design: docs/scoring.md, "Probe detection".

Someone holding a stolen card can resend a payment the model flags, changing the amount, e-mail,
device and time, until one variant is approved. E6 (docs/results/e6.md) simulated this and a
defense, built here exactly as Manisha simulated it (ml/experiments/e6_evasion.py, probe_frozen):

- A card is the proxy user id (card1 + addr1 + first-seen day); unknown cards (NA) aren't checked.
- Only the model's own flags count (review or decline). A payment this rule sends to review is
  saved as not flagged, so the rule never feeds itself and a card un-freezes.
- The window rolls: the card is held while it has PROBE_FLAGS or more model-flagged payments in
  the 24 h before the payment's own time. So the 6th payment after 5 flags is the first one held.
- A held payment goes to review; a decline stays a decline (the rule only changes approvals).
"""

PROBE_FLAGS = 5        # model-flagged payments on the card ...
PROBE_WINDOW = 86_400  # ... in this many seconds before the payment
REASON = f"probe: {PROBE_FLAGS} or more flagged payments on this card in 24 h"


def recent_flags(past, t, window=PROBE_WINDOW):
    """Model-flagged payments in `past` (store.history.to_past rows, all earlier than t) within
    `window` seconds before t. The same edge as E6: a flag exactly `window` seconds old counts."""
    return sum(1 for p in past if p.get("flagged") and p["t"] >= t - window)


def hold(decision, past, t, flags=PROBE_FLAGS, window=PROBE_WINDOW):
    """(final decision, held). Only an approval can be held, and it becomes a review."""
    if decision == "approve" and recent_flags(past, t, window) >= flags:
        return "review", True
    return decision, False
