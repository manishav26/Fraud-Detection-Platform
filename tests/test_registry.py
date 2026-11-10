import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ml.model import FILES
from registry import promote as promote_tool
from registry.register import checked_record, register
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
    (path / "meta.json").write_text('{"info": {"name": "fraud-lgbm-v1"}}')
    return path


class FakeApi:
    """model-admin-svc: records what it was sent, or refuses."""

    def __init__(self, refuse=None):
        self.sent, self.refuse = [], refuse

    def send(self, action, token):
        if self.refuse:
            raise RuntimeError(self.refuse)
        claims = verify_promotion(token, PUBLIC)
        self.sent.append((action, claims))
        return {"version": claims["version"], "label": claims["label"],
                "sequence": claims["sequence"], "by": "admin"}


GATE_PASS = {"passed": True, "against": "the pinned model", "pr_auc_gain": 0.02}
GATE_KEEP = {"passed": False, "against": "version 4", "pr_auc_gain": -0.001}


def run_promote(tmp_path, client, api=None, **overrides):
    api = api or FakeApi()
    args = {"download": lambda version, folder: model_folder(tmp_path),
            "validate": lambda folder: None, "gate": lambda api, folder: GATE_PASS,
            "confirm": lambda *a: True, **overrides}
    return promote_tool.promote(client, "3", PRIVATE, api, **args), api


def test_promote_sends_a_signed_message_with_label_and_gate(tmp_path):
    client = FakeClient()
    answer, api = run_promote(tmp_path, client, reason="tuned v1")
    action, claims = api.sent[0]
    assert action == "promote" and claims["version"] == "3"
    assert claims["sequence"] == answer["sequence"] and len(claims["sha256"]["model.txt"]) == 64
    assert claims["label"] == f"fraud-lgbm-v1-{claims['sha256']['model.txt'][:8]}"
    assert claims["gate"] == GATE_PASS and claims["reason"] == "tuned v1"
    assert client.aliases == {"production": "3"}


def test_later_promotion_has_a_higher_sequence(tmp_path):
    first, _ = run_promote(tmp_path, FakeClient())
    promote_tool.time.sleep(0.002)
    second, _ = run_promote(tmp_path, FakeClient())
    assert second["sequence"] > first["sequence"]


def test_gate_says_keep_stops_without_an_override(tmp_path):
    client = FakeClient()
    with pytest.raises(promote_tool.GateSaidKeep, match="OVERRIDE"):
        run_promote(tmp_path, client, gate=lambda api, folder: GATE_KEEP)
    assert client.aliases == {}


def test_override_promotes_and_records_the_gate_and_reason(tmp_path):
    _, api = run_promote(tmp_path, FakeClient(), gate=lambda api, folder: GATE_KEEP,
                         override=True, reason="demo: show a promotion")
    _, claims = api.sent[0]
    assert claims["gate"]["passed"] is False and claims["reason"] == "demo: show a promotion"


def test_rollback_skips_the_gate(tmp_path):
    def gate(api, folder):
        raise AssertionError("a rollback must not wait for the gate")

    _, api = run_promote(tmp_path, FakeClient(), action="rollback", reason="v5 bad", gate=gate)
    action, claims = api.sent[0]
    assert action == "rollback" and claims["action"] == "rollback" and claims["gate"] is None


def test_saying_no_changes_nothing(tmp_path):
    client = FakeClient()
    answer, api = run_promote(tmp_path, client, confirm=lambda *a: False)
    assert answer is None and api.sent == [] and client.aliases == {}


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


def test_alias_stays_put_if_model_admin_refuses(tmp_path):
    client = FakeClient()
    with pytest.raises(RuntimeError, match="replayed"):
        run_promote(tmp_path, client, api=FakeApi(refuse="replayed or old message"))
    assert client.aliases == {}


def test_gate_runs_against_the_pinned_model_until_something_is_promoted(tmp_path):
    seen = []

    class Api:
        def __init__(self, live):
            self._live = live

        def live(self):
            return {"live": self._live}

    def gate(candidate, current, against):
        seen.append((Path(current), against))
        return GATE_PASS

    promote_tool.gate_against_live(Api(None), tmp_path, gate=gate)
    promote_tool.gate_against_live(Api({"version": "4", "label": "fraud-lgbm-v1-535eb73c"}),
                                   tmp_path, gate=gate,
                                   download=lambda version, tmp: Path(tmp) / f"v{version}")
    assert seen[0] == (promote_tool.PINNED, "the pinned model")
    assert seen[1][0].name == "v4" and "version 4" in seen[1][1]


# ---------- register ----------

def test_broken_model_is_not_uploaded(tmp_path):
    def broken(folder):
        raise ValueError("bad model")

    with pytest.raises(ValueError):
        register(None, model_folder(tmp_path), validate=broken)   # client is never touched


def test_a_folder_that_fails_the_check_gets_no_training_record(tmp_path):
    """The hash check comes first: a tampered folder is never opened by LightGBM or evaluated."""
    recorded = []

    def broken(folder):
        raise ValueError("model.txt does not match its SHA-256 in manifest.json")

    with pytest.raises(ValueError, match="manifest"):
        checked_record(model_folder(tmp_path), validate=broken,
                       record=lambda folder, with_metrics: recorded.append(folder))
    assert recorded == []


def test_a_folder_that_passes_gets_its_record(tmp_path):
    folder = model_folder(tmp_path)
    record = checked_record(folder, with_metrics=False, validate=lambda f: None,
                            record=lambda f, with_metrics: {"folder": f, "metrics": with_metrics})
    assert record == {"folder": folder, "metrics": False}

