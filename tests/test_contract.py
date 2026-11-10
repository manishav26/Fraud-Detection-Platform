"""features/contract.py: the attrs contract, and that everything else agrees with it (ST5 found
three copies that disagreed; docs/ingest.md, "Input")."""

import json
import math
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from features.contract import ATTRS, NUMBER_ATTRS, TEXT_ATTRS, TEXT_MAX, Attrs, as_sent

ROOT = Path(__file__).resolve().parents[1]
MODEL_META = ROOT / "models" / "fraud-lgbm-v1" / "meta.json"
DATA = ROOT / "data" / "processed"


def test_51_attrs_15_of_them_text():
    assert len(ATTRS) == 51 and len(TEXT_ATTRS) == 15 and len(NUMBER_ATTRS) == 36


@pytest.mark.parametrize("bad", [
    {"D15": "AAA"},                 # what ST5 found: text in a number attr
    {"card1": "9500"},              # numbers are JSON numbers, never text
    {"card1": True},                # nor true/false
    {"D1": float("nan")}, {"D1": float("inf")},
    {"card4": 5},                   # text attrs are text
    {"card4": "x" * (TEXT_MAX + 1)},
    {"not_an_attr": 1},             # unknown attrs are refused
])
def test_input_that_breaks_the_contract_is_refused(bad):
    with pytest.raises(ValidationError):
        Attrs(**bad)


def test_valid_input_is_kept_as_sent():
    sent = {"card1": 9500, "addr1": 204.0, "card4": "visa", "D15": None}
    assert as_sent(Attrs(**sent)) == {"card1": 9500.0, "addr1": 204.0, "card4": "visa", "D15": None}
    assert as_sent(Attrs()) == {}


def test_the_openapi_file_says_the_same():
    api = yaml.safe_load((ROOT / "docs" / "openapi.yaml").read_text())
    attrs = (api["paths"]["/v1/transactions/score"]["post"]["requestBody"]["content"]
             ["application/json"]["schema"]["properties"]["attrs"])
    assert attrs["additionalProperties"] is False
    props = attrs["properties"]
    assert set(props) == set(ATTRS)
    assert {a for a, p in props.items() if p["type"] == "string"} == TEXT_ATTRS
    assert {a for a, p in props.items() if p["type"] == "number"} == set(NUMBER_ATTRS)
    assert all(props[a]["maxLength"] == TEXT_MAX for a in TEXT_ATTRS)


@pytest.mark.skipif(not MODEL_META.exists(), reason="no trained model in models/ (CI)")
def test_the_models_categorical_features_are_the_text_attrs():
    """A retrain that changes which features are categorical must change the contract too."""
    meta = json.loads(MODEL_META.read_text())
    assert set(meta["categories"]) == TEXT_ATTRS
    assert set(ATTRS) <= set(meta["features"])


@pytest.mark.skipif(not (DATA / "stream.parquet").exists(), reason="no data/processed (CI)")
@pytest.mark.parametrize("split", ["train", "val", "stream"])
def test_every_real_payment_passes_the_contract(split):
    import pandas as pd
    df = pd.read_parquet(DATA / f"{split}.parquet", columns=ATTRS)
    for a in TEXT_ATTRS:   # types and length checked column by column: fast, and the same rule
        values = df[a].dropna()
        assert values.map(lambda v: isinstance(v, str)).all() and values.str.len().max() <= TEXT_MAX
    for a in NUMBER_ATTRS:
        assert pd.api.types.is_numeric_dtype(df[a]), a
        assert not df[a].map(lambda v: isinstance(v, float) and math.isinf(v)).any(), a
    sample = df.sample(2000, random_state=0).astype(object).where(df.notna(), None)
    for row in sample.to_dict("records"):
        Attrs(**{k: (v.item() if hasattr(v, "item") else v) for k, v in row.items()})
