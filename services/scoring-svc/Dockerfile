# Built from the repo root (compose: context "."), because scoring uses features/, ml/ and store/.
FROM python:3.11-slim
# LightGBM needs the OpenMP runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY services/scoring-svc/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY features features
COPY ml/model.py ml/model.py
COPY store store
COPY observability observability
# Only what scoring needs to check and load promotions, not the register and promote tools
COPY registry/__init__.py registry/signing.py registry/live_model.py registry/
COPY config config
COPY services/scoring-svc/app app
# The model is not baked in: it is mounted at /models and checked against the pinned SHA-256.
# Keep idle connections 75 s, longer than Kong keeps them (60 s), so Kong always closes first.
# uvicorn's default 5 s let Kong reuse a connection uvicorn was closing: a 502 (docs/k8s.md).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "75"]
