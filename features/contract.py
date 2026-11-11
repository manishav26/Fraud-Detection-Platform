"""The payment's attrs, as features/schema.yaml defines them: one strict model, used by ingest and
scoring (week 5; ST5 found the gap, docs/ingest.md "Input").

    from features.contract import Attrs
    class Transaction(BaseModel):
        attrs: Attrs = Attrs()

Each of the 51 attrs is optional and may be null (the model handles missing values). Numbers must
be JSON numbers: text, true/false, NaN and Infinity are refused. Text attrs must be JSON text of
at most attr_text_max_length characters. Any other key is refused. Before this, any key took text
or a number, and text in a number attr crashed the model's encoder (a 500).

PaymentTime is the payment's ts: a time with a real time zone offset, in a sane range, turned
into UTC (utc_text gives the one form ingest stores). Before, any year and any offset Python
accepts went through, and the Spark feature job crash-looped on one it couldn't read.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import AfterValidator, AwareDatetime, ConfigDict, Field, create_model

SCHEMA = Path(__file__).resolve().parent / "schema.yaml"


def load(path=SCHEMA):
    """(all attrs in order, the text attrs, the text length limit) from the contract file."""
    schema = yaml.safe_load(Path(path).read_text())
    attrs = [c for cols in schema["attrs"].values() if isinstance(cols, list) for c in cols]
    text = set(schema["text_attrs"])
    unknown = text - set(attrs)
    if unknown:
        raise ValueError(f"text_attrs not among the attrs: {sorted(unknown)}")
    return attrs, text, int(schema["attr_text_max_length"])


ATTRS, TEXT_ATTRS, TEXT_MAX = load()


def _offset(text):
    sign = -1 if text.startswith("-") else 1
    hours, minutes = text.lstrip("+-").split(":")
    return sign * timedelta(hours=int(hours), minutes=int(minutes))


_TIME = yaml.safe_load(SCHEMA.read_text())["payment_time"]
MIN_OFFSET, MAX_OFFSET = _offset(_TIME["min_offset"]), _offset(_TIME["max_offset"])
MIN_YEAR, MAX_YEAR = int(_TIME["min_year"]), int(_TIME["max_year"])


def check_payment_time(value: datetime) -> datetime:
    """A time zone offset of a real zone, and a year in range (in UTC); the time, in UTC."""
    offset = value.utcoffset()
    if offset is None or not MIN_OFFSET <= offset <= MAX_OFFSET:
        raise ValueError(f"the time zone offset must be between {_TIME['min_offset']} and "
                         f"{_TIME['max_offset']}")
    utc = value.astimezone(UTC)
    if not MIN_YEAR <= utc.year <= MAX_YEAR:
        raise ValueError(f"the time must be in the years {MIN_YEAR}-{MAX_YEAR}")
    return utc


PaymentTime = Annotated[AwareDatetime, AfterValidator(check_payment_time)]


def utc_text(value):
    """The one form a payment's time is stored in: ISO 8601 in UTC, e.g. 2017-12-02T00:00:00Z."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


NUMBER_ATTRS = [a for a in ATTRS if a not in TEXT_ATTRS]

Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Text = Annotated[str, Field(strict=True, max_length=TEXT_MAX)]

Attrs = create_model(
    "Attrs",
    __config__=ConfigDict(extra="forbid"),
    **{a: (Text | None if a in TEXT_ATTRS else Number | None, None) for a in ATTRS},
)
Attrs.__doc__ = "The 51 raw IEEE-CIS attrs of a payment, typed as features/schema.yaml says."


def as_sent(attrs):
    """The attrs the client sent, as a plain dict: keys it left out stay left out."""
    return attrs.model_dump(exclude_unset=True)
