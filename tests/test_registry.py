import base64
import json
import os
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ml.model import FILES
from registry import promote as promote_tool
from registry.register import register
from registry.signing import (
    MODEL_FILES,
    InvalidPromotion,
    sign_promotion,
    verify_promotion,
)

HASHES = {"model.txt": "a" * 64, "meta.json": "b" * 64}


def key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


PRIVATE, PUBLIC = key_pair()


def b64(data):
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def good_token():
    return sign_promotion("fraud-lgbm", 3, HASHES, 1000, PRIVATE)


# ---------- signed messages ----------

def test_files_go_through_the_mlflow_server_not_direct_links():
    assert os.environ["MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD"] == "false"
    assert os.environ["MLFLOW_ENABLE_PROXY_MULTIPART_UPLOAD"] == "false"


def test_model_files_match_what_fraudmodel_checks():
    assert list(MODEL_FILES) == list(FILES)


def test_genuine_message_is_accepted():
    claims = verify_promotion(good_token(), PUBLIC)
    assert claims["version"] == "3" and claims["sequence"] == 1000 and claims["sha256"] == HASHES


def test_message_signed_with_another_key_is_refused():
    other_private, _ = key_pair()
    forged = sign_promotion("fraud-lgbm", 3, HASHES, 1000, other_private)
    with pytest.raises(InvalidPromotion):
        verify_promotion(forged, PUBLIC)


def test_changed_message_is_refused():
    header, payload, signature = good_token().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["version"] = "9"
    with pytest.raises(InvalidPromotion):
        verify_promotion(f"{header}.{b64(claims)}.{signature}", PUBLIC)


def test_alg_none_is_refused():
    token = (f"{b64({'alg': 'none', 'typ': 'JWT'})}."
             f"{b64({'iss': 'fraud-platform-promoter', 'aud': 'scoring-svc', 'model': 'm', 'version': '1', 'sha256': HASHES, 'sequence': 1})}.")
    with pytest.raises(InvalidPromotion):
        verify_promotion(token, PUBLIC)


def test_login_token_style_message_is_refused():
    # Right key, but not meant for scoring: no audience or promotion fields
    token = jwt.encode({"iss": "fraud-platform-issuer", "scope": "score:write"}, PRIVATE,
                       algorithm="RS256")
    with pytest.raises(InvalidPromotion):
        verify_promotion(token, PUBLIC)


@pytest.mark.parametrize("sha256, sequence", [
    ({"model.txt": "a" * 64}, 1),      # a file's hash is missing
    ("not a dict", 1),
    (HASHES, "1"),                     # sequence must be a number
    (HASHES, True),
])
def test_incomplete_message_is_refused(sha256, sequence):
    with pytest.raises(InvalidPromotion):
        verify_promotion(sign_promotion("m", 1, sha256, sequence, PRIVATE), PUBLIC)


# ---------- promote ----------

class FakeClient:
    def __init__(self, status="READY"):
        self.status, self.aliases = status, {}

    def get_model_version(self, name, version):
        return SimpleNamespace(status=self.status)

    def set_registered_model_alias(self, name, alias, version):
        self.aliases[alias] = version


def model_folder(path):
    (path / "model.txt").write_text("tree data")
    (path / "meta.json").write_text("{}")
    return path


def run_promote(tmp_path, client, **overrides):
    published = []
    args = {"download": lambda version, folder: model_folder(tmp_path),
            "validate": lambda folder: None, "publish": published.append,
            "confirm": lambda version, hashes: True, **overrides}
    return promote_tool.promote(client, "3", PRIVATE, **args), published


def test_promote_publishes_a_valid_message_and_moves_the_alias(tmp_path):
    client = FakeClient()
    sequence, published = run_promote(tmp_path, client)
    claims = verify_promotion(published[0], PUBLIC)
    assert claims["version"] == "3" and claims["sequence"] == sequence
    assert len(claims["sha256"]["model.txt"]) == 64
    assert client.aliases == {"production": "3"}


def test_later_promotion_has_a_higher_sequence(tmp_path):
    first, _ = run_promote(tmp_path, FakeClient())
    promote_tool.time.sleep(0.002)
    second, _ = run_promote(tmp_path, FakeClient())
    assert second > first


def test_saying_no_changes_nothing(tmp_path):
    client = FakeClient()
    sequence, published = run_promote(tmp_path, client, confirm=lambda v, h: False)
    assert sequence is None and published == [] and client.aliases == {}


def test_version_not_ready_is_refused(tmp_path):
    client = FakeClient(status="PENDING_REGISTRATION")
    with pytest.raises(RuntimeError, match="not READY"):
        run_promote(tmp_path, client)
    assert client.aliases == {}


def test_model_that_does_not_load_is_never_signed(tmp_path):
    def broken(folder):
        raise ValueError("model.txt does not match")

    client = FakeClient()
    with pytest.raises(ValueError):
        run_promote(tmp_path, client, validate=broken)
    assert client.aliases == {}


def test_alias_stays_put_if_publishing_fails(tmp_path):
    def down(token):
        raise TimeoutError

    client = FakeClient()
    with pytest.raises(TimeoutError):
        run_promote(tmp_path, client, publish=down)
    assert client.aliases == {}


# ---------- register ----------

def test_broken_model_is_not_uploaded(tmp_path):
    def broken(folder):
        raise ValueError("bad model")

    with pytest.raises(ValueError):
        register(None, model_folder(tmp_path), validate=broken)   # client is never touched
