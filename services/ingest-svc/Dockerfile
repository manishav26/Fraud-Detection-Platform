# Built from the repo root (compose: context "."), because it uses observability/.
FROM python:3.11-slim
WORKDIR /srv
COPY services/ingest-svc/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY observability observability
COPY services/ingest-svc/app app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
