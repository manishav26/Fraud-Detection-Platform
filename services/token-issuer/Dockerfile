# Built from the repo root (compose: context "."), because it uses observability/.
FROM python:3.11-slim
WORKDIR /srv
COPY services/token-issuer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY observability observability
COPY services/token-issuer/app app
# Keep idle connections 75 s, longer than Kong keeps them (60 s), so Kong always closes first.
# uvicorn's default 5 s let Kong reuse a connection uvicorn was closing: a 502 (docs/k8s.md).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "75"]
