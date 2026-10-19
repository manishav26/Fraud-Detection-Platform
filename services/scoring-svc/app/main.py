from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="scoring-svc (stub)")


class ScoreRequest(BaseModel):
    txn_id: str
    user_id: str
    amount: float
    attrs: dict[str, float | str | None] = {}


@app.post("/score")
def score(req: ScoreRequest):
    # Stub: fixed score until Manisha's model is wired in (week 3).
    return {"txn_id": req.txn_id, "score": 0.12, "model_version": "stub-0"}
