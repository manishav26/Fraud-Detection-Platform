FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY entrypoint.sh ensure_bucket.py smoke_test.py ./
# The registry needs no root rights
RUN useradd --create-home mlflow
USER mlflow
ENTRYPOINT ["sh", "/srv/entrypoint.sh"]
