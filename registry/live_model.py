"""The model scoring is using right now, and how it changes. Design: docs/mlflow.md

Only a genuine, newer promotion message whose files match its hashes can replace the model. If
anything is wrong, the current model keeps serving: a bad promotion must never stop scoring.
"""

import asyncio
import logging
import tempfile
import time

from aiokafka import AIOKafkaConsumer, TopicPartition

from registry.signing import (
    MODEL_FILES,
    MODEL_NAME,
    TOPIC,
    InvalidPromotion,
    model_hashes,
    verify_promotion,
)

log = logging.getLogger("scoring.live_model")

RETRY_SECONDS = 30      # how often to retry a promotion that failed for a temporary reason
RECONNECT_SECONDS = 5   # wait before reconnecting to Redpanda after an error


class RefusedModel(Exception):
    """The files don't match the signed hashes: tampered with, never to be retried."""


def download_version(version, folder):
    # Imported here: mlflow is slow to import and uses memory, and only a promotion needs it
    import mlflow

    return mlflow.artifacts.download_artifacts(f"models:/{MODEL_NAME}/{version}", dst_path=folder)


def version_label(version):
    return f"{MODEL_NAME}-v{version}"


class LiveModel:
    def __init__(self, model, version, public_key, load, download=download_version):
        # One attribute holding both, replaced in a single step. A request takes one copy of it,
        # so it can never pair the new model with the old version name.
        self.current = (model, version)
        self.sequence = None   # of the promotion in use; None = started from the pinned model
        self.public_key, self.load, self.download = public_key, load, download

    def _candidates(self, tokens):
        """Genuine messages newer than the current one, newest first."""
        found = []
        for token in tokens:
            try:
                claims = verify_promotion(token, self.public_key)
            except InvalidPromotion as e:
                log.warning("refused promotion message: %s", e)
                continue
            if claims["model"] != MODEL_NAME:
                log.warning("refused promotion for another model: %s", claims["model"])
            elif self.sequence is not None and claims["sequence"] < self.sequence:
                log.warning("refused old promotion: sequence %s is below the current %s",
                            claims["sequence"], self.sequence)
            elif self.sequence is None or claims["sequence"] > self.sequence:
                found.append((claims, token))
        return sorted(found, key=lambda pair: pair[0]["sequence"], reverse=True)

    def _switch(self, claims):
        start = time.perf_counter()
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.download(claims["version"], tmp)
            downloaded = time.perf_counter()
            actual = model_hashes(folder)
            for f in MODEL_FILES:
                if actual[f] != claims["sha256"][f]:
                    raise RefusedModel(f"{f} of version {claims['version']} does not match "
                                       "its signed SHA-256")
            model = self.load(folder)   # fully loaded before anything changes
        self.current = (model, version_label(claims["version"]))
        self.sequence = claims["sequence"]
        log.warning("swap timing: download %.2f s, check and load %.2f s",
                    downloaded - start, time.perf_counter() - downloaded)

    def apply_latest(self, tokens):
        """Switch to the newest usable promotion. Returns the messages to try again later."""
        for claims, token in self._candidates(tokens):
            try:
                self._switch(claims)
            except RefusedModel as e:
                log.error("refused model, keeping %s: %s", self.current[1], e)
                continue   # an older genuine promotion may still be newer than what we run
            except Exception:
                # For example MLflow is down: nothing is wrong with the message, so retry
                log.exception("could not load version %s, keeping %s",
                              claims["version"], self.current[1])
                return [token]
            log.warning("now serving %s (sequence %s)", self.current[1], self.sequence)
            return []
        return []


def _values(batches):
    """Message texts from a getmany() result. Empty values are deletion markers: skip them."""
    return [r.value.decode(errors="replace") for records in batches.values() for r in records
            if r.value is not None]


async def follow_promotions(live, bootstrap, caught_up):
    """Runs for the life of the service. Every pod reads the whole topic by itself (no consumer
    group), because all pods must act on every message. Survives Redpanda being down or the topic
    not existing yet: it keeps serving the current model and tries again.
    """
    retry = []
    while True:
        consumer = AIOKafkaConsumer(bootstrap_servers=bootstrap, auto_offset_reset="earliest")
        try:
            await consumer.start()
            partition = TopicPartition(TOPIC, 0)   # the topic has 1 partition (docs/topics.md)
            consumer.assign([partition])
            await consumer.seek_to_beginning(partition)
            end = (await consumer.end_offsets([partition]))[partition]

            # Read everything so far, then apply only the newest: no need to load each old model
            tokens = []
            while await consumer.position(partition) < end:
                tokens += _values(await consumer.getmany(timeout_ms=1000))
            retry = await asyncio.to_thread(live.apply_latest, tokens + retry)
            caught_up.set()

            while True:
                tokens = _values(await consumer.getmany(timeout_ms=RETRY_SECONDS * 1000))
                if tokens or retry:
                    # In a thread: downloading and loading must not block other work
                    retry = await asyncio.to_thread(live.apply_latest, tokens + retry)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("lost contact with %s, retrying", TOPIC)
            await asyncio.sleep(RECONNECT_SECONDS)
        finally:
            await consumer.stop()
