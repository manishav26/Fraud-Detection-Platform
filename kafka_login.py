"""Redpanda logins for our Kafka clients (week 5; design: docs/kafka.md).

On the cluster every workload has its own Redpanda user, and topic permissions (ACLs) limit it to
the topics it needs. Its name and password come from its Secret as KAFKA_USER and
KAFKA_PASSWORD. With no KAFKA_USER the client logs in as no one, which is what compose's open
Redpanda expects (dev only, like its dev passwords).

A client must match the broker: Redpanda refuses a login while its logins are off
(ILLEGAL_SASL_STATE), and a client without one once they're on. So scripts/k8s_up.sh switches
logins on first, then restarts the services with theirs.

SASL_PLAINTEXT: the password is checked with SCRAM (it never crosses the wire), and the
connection itself is already encrypted by Linkerd's mTLS (docs/mtls.md).
"""

import os

MECHANISM = "SCRAM-SHA-256"


def _login(env):
    user = (env if env is not None else os.environ).get("KAFKA_USER")
    if not user:
        return None
    password = (env if env is not None else os.environ).get("KAFKA_PASSWORD")
    if not password:
        raise RuntimeError("KAFKA_USER is set but KAFKA_PASSWORD is not")
    return user, password


def aiokafka_options(env=None):
    """Extra arguments for AIOKafkaProducer / AIOKafkaConsumer: {} without a login."""
    login = _login(env)
    if login is None:
        return {}
    return {"security_protocol": "SASL_PLAINTEXT", "sasl_mechanism": MECHANISM,
            "sasl_plain_username": login[0], "sasl_plain_password": login[1]}


def spark_options(env=None):
    """Extra options for Spark's Kafka source: {} without a login. Kafka's client prints the
    JAAS setting as [hidden], and Spark redacts option values that contain "password"."""
    login = _login(env)
    if login is None:
        return {}
    user, password = login
    jaas = ("org.apache.kafka.common.security.scram.ScramLoginModule required "
            f'username="{user}" password="{password}";')
    return {"kafka.security.protocol": "SASL_PLAINTEXT", "kafka.sasl.mechanism": MECHANISM,
            "kafka.sasl.jaas.config": jaas}
