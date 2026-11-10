# Built from the repo root (compose: context "."), because it uses observability/.
FROM python:3.11-slim
# Security updates, and newer packaging tools than the base image has: Trivy found HIGH CVEs in
# its zlib and in the copies of wheel and jaraco.context bundled in setuptools 79 (docs/supply-chain.md)
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --disable-pip-version-check "setuptools==84.2.1" "wheel==0.48.0"
WORKDIR /srv
COPY services/siem-mock/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY observability observability
COPY services/siem-mock/app app
# Not root (Trivy DS-0002): code that escapes the app gets no root inside the container
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001
# Keep idle connections 75 s: alert-svc reuses its connection for every batch
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "75"]
