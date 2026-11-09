# The streaming feature job (week 4, step 1): Spark Structured Streaming in local mode, plus the
# plain Python writer that shares its code. Built from the repo root: it uses features/ and
# stream/. Design: docs/spark.md
FROM python:3.11-slim
# Security updates and newer packaging tools (docs/supply-chain.md); Java 21 for Spark 4
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends openjdk-21-jre-headless curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --disable-pip-version-check "setuptools==84.2.1" "wheel==0.48.0"
# The Kafka source for Spark and what it needs, pinned to the versions Spark 4.2.1 was built
# against (its pom), fetched now so nothing is downloaded while the job runs
ARG SPARK=4.2.1
ARG MAVEN=https://repo1.maven.org/maven2
RUN mkdir -p /opt/spark-jars && cd /opt/spark-jars \
    && curl -fsSLO $MAVEN/org/apache/spark/spark-sql-kafka-0-10_2.13/$SPARK/spark-sql-kafka-0-10_2.13-$SPARK.jar \
    && curl -fsSLO $MAVEN/org/apache/spark/spark-token-provider-kafka-0-10_2.13/$SPARK/spark-token-provider-kafka-0-10_2.13-$SPARK.jar \
    && curl -fsSLO $MAVEN/org/apache/kafka/kafka-clients/3.9.2/kafka-clients-3.9.2.jar \
    && curl -fsSLO $MAVEN/org/apache/commons/commons-pool2/2.13.1/commons-pool2-2.13.1.jar
ENV SPARK_JARS=/opt/spark-jars/spark-sql-kafka-0-10_2.13-4.2.1.jar,/opt/spark-jars/spark-token-provider-kafka-0-10_2.13-4.2.1.jar,/opt/spark-jars/kafka-clients-3.9.2.jar,/opt/spark-jars/commons-pool2-2.13.1.jar \
    PYTHONUNBUFFERED=1
WORKDIR /srv
COPY services/feature-job/requirements.txt services/feature-job/patch_jars.sh ./
# Then fix the vulnerable Java libraries PySpark ships (patch_jars.sh), and drop curl
RUN pip install --no-cache-dir -r requirements.txt \
    && sh patch_jars.sh && rm patch_jars.sh \
    && apt-get purge -y curl && apt-get autoremove -y
COPY features features
COPY stream stream
# Not root (Trivy DS-0002). HOME on /tmp: Spark and Java write small files under it
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001
ENV HOME=/tmp
CMD ["python", "-m", "stream.run"]
