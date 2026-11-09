from pathlib import Path

import yaml
from app.main import Decision  # ingest-svc

from ml.model import FraudModel


def test_decision_thresholds_come_from_config():
    # scoring-svc decides with model.decide; the thresholds are config/decision.yaml
    thresholds = yaml.safe_load(Path("config/decision.yaml").read_text())
    model = FraudModel.__new__(FraudModel)
    model.meta = {"thresholds": thresholds}
    assert model.decide(0.1) == "approve"
    assert model.decide(thresholds["review_threshold"]) == "review"
    middle = (thresholds["review_threshold"] + thresholds["decline_threshold"]) / 2
    assert model.decide(middle) == "review"
    assert model.decide(thresholds["decline_threshold"]) == "decline"


def pins(path):
    """{package: version} from a requirements file; every line must be pinned with ==."""
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            assert "==" in line, f"{path}: {line} has no exact version"
            name, version = line.split("==")
            out[name.split("[")[0].lower()] = version
    return out


# Deliberate differences from dev, each with its reason
DIFFERENT_FROM_DEV = {
    # PySpark 4.2 warns it doesn't fully support pandas 3 yet (docs/spark.md)
    ("feature-job", "pandas"),
}


def test_every_dependency_is_pinned_and_services_match_dev():
    dev = pins("requirements-dev.txt")
    for req in Path("services").glob("*/requirements.txt"):
        for name, version in pins(req).items():
            if name in dev and (req.parent.name, name) not in DIFFERENT_FROM_DEV:
                assert version == dev[name], f"{req}: {name} {version} != dev {dev[name]}"


def test_ingest_passes_scoring_decision_through():
    d = Decision(txn_id="t1", score=0.8, decision="decline", model_version="v1")
    assert d.decision == "decline" and d.cold_start is False
