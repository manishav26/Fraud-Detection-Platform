"""kafka_login.py: Redpanda logins for our Kafka clients (docs/kafka.md)."""

import pytest

from kafka_login import aiokafka_options, spark_options


def test_no_user_means_no_login_as_compose_expects():
    assert aiokafka_options({}) == {} and spark_options({}) == {}
    assert aiokafka_options({"KAFKA_USER": ""}) == {}


def test_aiokafka_logs_in_with_scram():
    opts = aiokafka_options({"KAFKA_USER": "ingest", "KAFKA_PASSWORD": "pw"})
    assert opts == {"security_protocol": "SASL_PLAINTEXT", "sasl_mechanism": "SCRAM-SHA-256",
                    "sasl_plain_username": "ingest", "sasl_plain_password": "pw"}


def test_spark_gets_the_same_login():
    opts = spark_options({"KAFKA_USER": "feature-job", "KAFKA_PASSWORD": "pw"})
    assert opts["kafka.security.protocol"] == "SASL_PLAINTEXT"
    assert opts["kafka.sasl.mechanism"] == "SCRAM-SHA-256"
    assert 'username="feature-job" password="pw";' in opts["kafka.sasl.jaas.config"]


def test_a_user_without_a_password_is_an_error_not_an_anonymous_login():
    with pytest.raises(RuntimeError):
        aiokafka_options({"KAFKA_USER": "ingest"})
