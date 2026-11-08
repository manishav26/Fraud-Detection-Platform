"""Signed promotion messages: the only thing that can make a model live. Design: docs/mlflow.md

A promotion is a JWT signed with a private key that only the promote tool holds. Scoring has just
the public key, so it can check a message but can't create one. The message says which version
goes live and the SHA-256 its files must have.

Check a message by hand (reads it from stdin, exit code 1 if it is not valid):
    python -m registry.signing < message.txt
"""

import hashlib
import json
import sys
from pathlib import Path

import jwt

MODEL_NAME = "fraud-lgbm"
TOPIC = "model.events"
ALGORITHM = "RS256"
ISSUER = "fraud-platform-promoter"
AUDIENCE = "scoring-svc"
MODEL_FILES = ("model.txt", "meta.json")   # the same files FraudModel checks at load
ROOT = Path(__file__).resolve().parents[1]
PUBLIC_KEY = ROOT / "promotion-keys" / "public.pem"
PRIVATE_KEY = ROOT / "promotion-keys" / "private.pem"


class InvalidPromotion(Exception):
    """The message must not be trusted."""


def model_hashes(folder):
    return {f: hashlib.sha256((Path(folder) / f).read_bytes()).hexdigest() for f in MODEL_FILES}


def model_label(folder, model_txt_sha256):
    """The `model_version` answers carry: the model's name plus the first 8 characters of its
    model.txt SHA-256, e.g. fraud-lgbm-v1-535eb73c. The same files always get the same label,
    whether pinned at startup or promoted, and different files never share one.

    Pass a hash that has already been checked (against the pins or a signed promotion), so the
    label never names files the service didn't verify.
    """
    meta = json.loads((Path(folder) / "meta.json").read_text())
    name = (meta.get("info") or {}).get("name") or MODEL_NAME
    return f"{name}-{model_txt_sha256[:8]}"


def sign_promotion(model, version, sha256, sequence, private_key, **extra):
    """No expiry on purpose: the latest message stays in the topic, and a restarting pod must
    still accept it. An old message is refused by its lower `sequence` instead.

    `extra` claims (week 4: action, label, reason, gate) are signed too, so the audit row that
    model-admin-svc writes from them says exactly what the key holder approved. Scoring ignores
    them.
    """
    return jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "model": model, "version": str(version),
                       "sha256": sha256, "sequence": sequence, **extra}, private_key,
                      algorithm=ALGORITHM)


def verify_promotion(token, public_key):
    """The claims of a genuine promotion, or InvalidPromotion.

    The algorithm is fixed here, never read from the message: otherwise a forged message could
    say `alg: none`, or use the public key as a shared secret.
    """
    try:
        claims = jwt.decode(token, public_key, algorithms=[ALGORITHM], audience=AUDIENCE,
                            issuer=ISSUER,
                            options={"require": ["iss", "aud", "model", "version", "sha256",
                                                 "sequence"]})
    except jwt.InvalidTokenError as e:
        raise InvalidPromotion(str(e)) from e
    sequence, sha256 = claims["sequence"], claims["sha256"]
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise InvalidPromotion("sequence is not a whole number")
    if not isinstance(sha256, dict) or not all(sha256.get(f) for f in MODEL_FILES):
        raise InvalidPromotion(f"message needs a SHA-256 for each of {MODEL_FILES}")
    return claims


def main():
    try:
        claims = verify_promotion(sys.stdin.read().strip(), PUBLIC_KEY.read_text())
    except InvalidPromotion as e:
        sys.exit(f"INVALID: {e}")
    print("VALID signature")
    print(json.dumps(claims, indent=2))


if __name__ == "__main__":
    main()
