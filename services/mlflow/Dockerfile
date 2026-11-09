FROM python:3.11-slim
# Security updates, and newer packaging tools than the base image has: Trivy found HIGH CVEs in
# its zlib and in the copies of wheel and jaraco.context bundled in setuptools 79 (docs/supply-chain.md)
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --disable-pip-version-check "setuptools==84.2.1" "wheel==0.48.0"
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY entrypoint.sh ensure_bucket.py smoke_test.py ./
# The registry needs no root rights
RUN useradd --create-home mlflow
USER mlflow
ENTRYPOINT ["sh", "/srv/entrypoint.sh"]
