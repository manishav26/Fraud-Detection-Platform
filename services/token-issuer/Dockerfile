# Built from the repo root (compose: context "."), because it uses observability/.
FROM python:3.11-slim
WORKDIR /srv
COPY services/token-issuer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY observability observability
COPY services/token-issuer/app app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
