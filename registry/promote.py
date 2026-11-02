"""Make a registered model version live, by publishing a signed message to model.events.

    make promote VERSION=1

Roll back by promoting the older version again: it gets a new, higher sequence number.
Design: docs/mlflow.md
"""

import argparse
import asyncio
import os
import tempfile
import time

import mlflow
from aiokafka import AIOKafkaProducer
from mlflow import MlflowClient

from ml.model import FraudModel
from registry.register import MODEL_NAME, TRACKING_URI, model_hashes
from registry.signing import PRIVATE_KEY, sign_promotion

ALIAS = "production"
TOPIC = "model.events"
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
SEND_TIMEOUT = 10.0   # seconds; aiokafka alone keeps retrying forever (docs/ingest.md)


def new_sequence():
    """Milliseconds since 1970: always higher than earlier promotions, with nothing to look up."""
    return int(time.time() * 1000)


def download(version, folder):
    return mlflow.artifacts.download_artifacts(f"models:/{MODEL_NAME}/{version}", dst_path=folder)


async def _publish(token):
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP, acks="all",
                                enable_idempotence=True)
    await producer.start()
    try:
        # key = model name: compaction keeps the latest message per key
        await asyncio.wait_for(producer.send_and_wait(TOPIC, token.encode(),
                                                      key=MODEL_NAME.encode()), SEND_TIMEOUT)
    finally:
        await producer.stop()


def publish(token):
    asyncio.run(_publish(token))


def confirm_with_human(version, hashes):
    print(f"About to make {MODEL_NAME} version {version} live. Files:")
    for f, h in hashes.items():
        print(f"  {f}  {h}")
    print("Compare these with the values the model's owner gave you.")
    return input("Sign and publish? [y/N] ").strip().lower() == "y"


def promote(client, version, private_key, download=download, validate=FraudModel.load,
            publish=publish, confirm=confirm_with_human):
    """Returns the sequence number, or None if the person said no. Raises if anything is wrong.

    The hashes are taken from the files just downloaded, then checked by a person: if someone
    replaced the files in the registry, this is the last point to catch it before signing.
    """
    status = client.get_model_version(MODEL_NAME, str(version)).status
    if status != "READY":
        raise RuntimeError(f"version {version} is {status}, not READY")

    with tempfile.TemporaryDirectory() as tmp:
        folder = download(version, tmp)
        validate(folder)   # a model that doesn't load must never be signed
        hashes = model_hashes(folder)
    if not confirm(version, hashes):
        return None

    sequence = new_sequence()
    publish(sign_promotion(MODEL_NAME, version, hashes, sequence, private_key))
    # After publishing: the message decides what is live, the alias only shows it to people.
    # If this line fails, the model is already live; running promote again repairs the alias.
    client.set_registered_model_alias(MODEL_NAME, ALIAS, str(version))
    return sequence


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", required=True)
    args = p.parse_args()
    mlflow.set_tracking_uri(TRACKING_URI)
    sequence = promote(MlflowClient(TRACKING_URI, TRACKING_URI), args.version,
                       PRIVATE_KEY.read_text())
    if sequence is None:
        print("Cancelled: nothing changed.")
    else:
        print(f"Published to {TOPIC}, sequence {sequence}. Alias {ALIAS} -> version {args.version}")


if __name__ == "__main__":
    main()
