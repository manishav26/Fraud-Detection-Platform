"""When scoring may and may not change its model (threat 11, ST9)."""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from registry.live_model import LiveModel, version_label
from registry.signing import model_hashes, sign_promotion


def key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


PRIVATE, PUBLIC = key_pair()
OLD = ("old model", "fraud-lgbm-v1")


@pytest.fixture
def registry(tmp_path):
    """A fake MLflow: version -> folder with that version's files."""
    versions = {}
    for version in ("2", "3"):
        folder = tmp_path / version
        folder.mkdir()
        (folder / "model.txt").write_text(f"trees of version {version}")
        (folder / "meta.json").write_text("{}")
        versions[version] = folder
    return versions


def live_model(registry, load=None, download=None):
    return LiveModel(*OLD, PUBLIC, load or (lambda folder: f"model from {folder.name}"),
                     download or (lambda version, tmp: registry[version]))


def message(registry, version="2", sequence=100, model="fraud-lgbm", key=PRIVATE):
    return sign_promotion(model, version, model_hashes(registry[version]), sequence, key)


def tamper(registry, version="2"):
    (registry[version] / "model.txt").write_text("attacker's model")


def test_genuine_promotion_switches_model_and_name(registry):
    live = live_model(registry)
    assert live.apply_latest([message(registry)]) == []
    assert live.current == ("model from 2", version_label("2"))
    assert live.sequence == 100


def test_forged_message_is_refused(registry):
    other_key, _ = key_pair()
    live = live_model(registry)
    assert live.apply_latest([message(registry, key=other_key)]) == []
    assert live.current == OLD


def test_junk_message_is_refused(registry):
    live = live_model(registry)
    assert live.apply_latest(["not a message"]) == []
    assert live.current == OLD


def test_promotion_for_another_model_is_refused(registry):
    live = live_model(registry)
    live.apply_latest([message(registry, model="another-model")])
    assert live.current == OLD


def test_swapped_file_is_refused_and_not_retried(registry):
    token = message(registry)
    tamper(registry)     # after signing: the registry's file no longer matches
    live = live_model(registry)
    assert live.apply_latest([token]) == []
    assert live.current == OLD and live.sequence is None


def test_replayed_old_message_cannot_bring_back_an_old_model(registry):
    live = live_model(registry)
    live.apply_latest([message(registry, "3", sequence=200)])
    live.apply_latest([message(registry, "2", sequence=100)])
    assert live.current[1] == version_label("3") and live.sequence == 200


def test_same_message_read_again_does_not_reload(registry):
    loads = []
    live = live_model(registry, load=lambda folder: loads.append(folder) or "model")
    token = message(registry)
    live.apply_latest([token])
    live.apply_latest([token])   # for example after reconnecting to Redpanda
    assert len(loads) == 1


def test_rollback_is_a_newer_promotion_of_an_older_version(registry):
    live = live_model(registry)
    live.apply_latest([message(registry, "3", sequence=200)])
    live.apply_latest([message(registry, "2", sequence=300)])
    assert live.current[1] == version_label("2") and live.sequence == 300


def test_several_messages_load_only_the_newest(registry):
    loads = []
    live = live_model(registry, load=lambda folder: loads.append(folder.name) or "model")
    live.apply_latest([message(registry, "2", sequence=100), message(registry, "3", sequence=200)])
    assert loads == ["3"] and live.sequence == 200


def test_tampered_newest_falls_back_to_older_genuine_promotion(registry):
    tokens = [message(registry, "2", sequence=100), message(registry, "3", sequence=200)]
    tamper(registry, "3")
    live = live_model(registry)
    live.apply_latest(tokens)
    assert live.current[1] == version_label("2") and live.sequence == 100


def test_mlflow_down_keeps_serving_and_retries(registry):
    def down(version, tmp):
        raise ConnectionError("MLflow is down")

    token = message(registry)
    live = live_model(registry, download=down)
    assert live.apply_latest([token]) == [token]   # try this one again later
    assert live.current == OLD

    live.download = lambda version, tmp: registry[version]   # MLflow is back
    assert live.apply_latest([token]) == []
    assert live.current[1] == version_label("2")


def test_model_that_fails_to_load_changes_nothing(registry):
    def broken(folder):
        raise ValueError("bad model file")

    live = live_model(registry, load=broken)
    live.apply_latest([message(registry)])
    assert live.current == OLD and live.sequence is None
