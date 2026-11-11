"""pseudonym.py: user ids -> HMAC pseudonyms (threat 8, docs/privacy.md)."""

import hashlib
import hmac

import pandas as pd

from features.history import HISTORY_FEATURES, add_history
from pseudonym import DEV_KEY, is_pseudonym, pseudonym
from store.history import is_known

RAW = "9500|204.0|98.0"


def test_same_id_same_pseudonym_and_different_ids_differ():
    assert pseudonym(RAW, "k") == pseudonym(RAW, "k")
    assert pseudonym(RAW, "k") != pseudonym("9501|204.0|98.0", "k")


def test_it_is_a_keyed_hmac_not_a_plain_hash():
    digest = hmac.new(b"k", RAW.encode(), hashlib.sha256).hexdigest()[:32]
    assert pseudonym(RAW, "k") == f"p1_{digest}"
    assert pseudonym(RAW, "k") != pseudonym(RAW, "another key")
    assert hashlib.sha256(RAW.encode()).hexdigest()[:32] not in pseudonym(RAW, "k")


def test_the_raw_id_doesnt_appear_in_it():
    p = pseudonym(RAW, "k")
    assert "9500" not in p.split("_", 1)[0] and "|" not in p and is_pseudonym(p)


def test_unknown_cards_stay_unknown_and_known_stay_known():
    unknown = pseudonym("9500|NA|98.0", "k")
    assert unknown.startswith("NA|p1_") and is_pseudonym(unknown)
    assert not is_known(unknown) and is_known(pseudonym(RAW, "k"))


def test_applying_it_twice_changes_nothing():
    once = pseudonym(RAW, "k")
    assert pseudonym(once, "k") == once
    assert pseudonym(pseudonym("9500|NA|98.0", "k"), "k") == pseudonym("9500|NA|98.0", "k")


def test_shapes_that_are_not_pseudonyms():
    for value in [RAW, "p1_xyz", "p1_" + "A" * 32, "p2_" + "a" * 32, "NA|" + RAW, ""]:
        assert not is_pseudonym(value), value


def test_the_dev_key_is_the_default(monkeypatch):
    monkeypatch.delenv("PSEUDONYM_KEY", raising=False)
    assert pseudonym(RAW) == pseudonym(RAW, DEV_KEY)
    monkeypatch.setenv("PSEUDONYM_KEY", "cluster key")
    assert pseudonym(RAW) == pseudonym(RAW, "cluster key")


def test_history_features_are_identical_under_pseudonyms():
    """The model's history features group payments by user only, so a one-to-one rename of the
    ids changes none of them: training's code on raw ids = on pseudonyms."""
    df = pd.DataFrame({
        "uid": ["9500|204.0|98.0"] * 3 + ["7000|330.0|5.0"] * 2,
        "uid_known": True,
        "TransactionDT": [100, 200, 4000, 150, 90_000],
        "TransactionAmt": [10.0, 20.0, 30.0, 5.0, 7.0],
        "P_emaildomain": ["a.com", "b.com", "a.com", None, "c.com"],
        "DeviceType": ["mobile"] * 5, "ProductCD": ["W"] * 5,
        "TransactionID": [1, 2, 3, 4, 5],
    })
    raw = add_history(df).sort_values("TransactionID").reset_index(drop=True)
    hidden = add_history(df.assign(uid=df["uid"].map(lambda u: pseudonym(u, "k"))))
    hidden = hidden.sort_values("TransactionID").reset_index(drop=True)
    pd.testing.assert_frame_equal(raw[HISTORY_FEATURES], hidden[HISTORY_FEATURES])
