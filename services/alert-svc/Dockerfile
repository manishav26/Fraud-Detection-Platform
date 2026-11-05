# Built from the repo root (compose: context "."), because alert-svc uses audit/.
FROM python:3.11-slim
WORKDIR /srv
COPY services/alert-svc/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY audit audit
COPY services/alert-svc/app app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
