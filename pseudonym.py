"""Pseudonyms for user ids (week 5, step 2; threat 8, ST11). Design: docs/privacy.md

ingest-svc replaces every user id with its pseudonym before anything is stored, so Redpanda,
Cassandra, cases and the SIEM only ever hold pseudonyms:

    9500|204.0|98.0   ->  p1_3f2a...          (32 hex characters)
    9500|NA|98.0      ->  NA|p1_c41b...       (unknown card: still unknown to is_known)

HMAC-SHA256 with a secret key, not a plain hash: without the key no one can test a guessed id.
One id always gives the same pseudonym, so a card's payments still group together and every
history feature is unchanged. It can't be turned back into the id; with the key, an id can be
turned into its pseudonym, to look a customer up (`make pseudonym ID=...`).

The key: PSEUDONYM_KEY, from the encrypted secrets file on the cluster (only ingest and the
laptop tools that write history get it). Compose and unit tests use the dev key below, like
compose's dev passwords; the cluster's secrets check refuses it there.
"""

import hashlib
import hmac
import os

VERSION = "p1"      # the key's version: a new key would need every stored id re-keyed
HEX = 32            # 128 bits
DEV_KEY = "pseudonym_dev_only"
UNKNOWN_PART = "NA"   # features/proxy_id.py writes this for a missing part (store.history)


def key():
    return os.getenv("PSEUDONYM_KEY", DEV_KEY)


def is_pseudonym(value):
    known = value.removeprefix(f"{UNKNOWN_PART}|")
    head, _, digest = known.partition("_")
    return (head == VERSION and len(digest) == HEX
            and all(c in "0123456789abcdef" for c in digest))


def pseudonym(user_id, secret=None):
    """The pseudonym of a raw user id. A pseudonym is returned unchanged, so applying it twice
    (a retried payment, a tool run on data that is already converted) is safe."""
    if is_pseudonym(user_id):
        return user_id
    secret = (secret if secret is not None else key()).encode()
    digest = hmac.new(secret, user_id.encode(), hashlib.sha256).hexdigest()[:HEX]
    unknown = UNKNOWN_PART in user_id.split("|")
    return f"{UNKNOWN_PART}|{VERSION}_{digest}" if unknown else f"{VERSION}_{digest}"


if __name__ == "__main__":   # make pseudonym ID='9500|204.0|98.0'   (make k8s-pseudonym: the cluster's key)
    import sys
    print(pseudonym(sys.argv[1]))
