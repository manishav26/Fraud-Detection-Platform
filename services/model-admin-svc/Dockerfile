# Built from the repo root (compose: context "."), because model-admin-svc uses audit/ and registry/.
FROM python:3.11-slim
WORKDIR /srv
COPY services/model-admin-svc/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY audit audit
COPY observability observability
# Only the code that checks a promotion: signing needs the private key, which never comes here
COPY registry/__init__.py registry/signing.py registry/
COPY services/model-admin-svc/app app
# Keep idle connections 75 s, longer than Kong keeps them (60 s), so Kong always closes first.
# uvicorn's default 5 s let Kong reuse a connection uvicorn was closing: a 502 (docs/k8s.md).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "75"]
