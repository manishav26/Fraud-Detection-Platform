"""Add a trained model folder to the MLflow registry as a new version.

    make register MODEL_FOLDER=models/fraud-lgbm-v1

Registering does not make a model live: that takes `make promote`, and the promotion key.
Each version's run also records how the model was trained (ml/train/training_record.py):
its settings, data windows, test metrics on days 92-109 and the git commit.
"""

import argparse
import os

import mlflow
from mlflow import MlflowClient

from ml.model import FraudModel
from ml.train.training_record import log_record, training_record
from registry.signing import MODEL_NAME, model_hashes

TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5050")


def register(client, folder, validate=FraudModel.load, record=None):
    """Returns (version, hashes). A folder that doesn't load is refused before any upload.
    `record` (from training_record) is logged on the run as params, metrics and tags."""
    validate(folder)
    hashes = model_hashes(folder)

    experiment = client.get_experiment_by_name(MODEL_NAME)
    experiment_id = experiment.experiment_id if experiment else client.create_experiment(MODEL_NAME)
    run_id = client.create_run(experiment_id).info.run_id
    if record:
        log_record(client, run_id, record)
    client.log_artifacts(run_id, str(folder), "model")
    client.set_terminated(run_id)

    if not client.search_registered_models(f"name='{MODEL_NAME}'"):
        client.create_registered_model(MODEL_NAME)
    source = f"{client.get_run(run_id).info.artifact_uri}/model"
    version = client.create_model_version(MODEL_NAME, source, run_id=run_id).version
    return version, hashes


def checked_record(folder, with_metrics=True, validate=FraudModel.load, record=training_record):
    """The folder's training record, made only after the folder passes the same check register()
    makes: its files must match manifest.json. A tampered or broken folder is refused here, before
    LightGBM opens its model.txt and before the 1-2 minute evaluation."""
    validate(folder)
    return record(folder, with_metrics=with_metrics)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--folder", required=True)
    p.add_argument("--no-metrics", action="store_true",
                   help="skip measuring the model on days 92-109 (saves 1-2 minutes)")
    args = p.parse_args()
    print("recording how the model was trained"
          + ("" if args.no_metrics else " (measuring it on days 92-109, 1-2 minutes)") + "...")
    record = checked_record(args.folder, with_metrics=not args.no_metrics)
    mlflow.set_tracking_uri(TRACKING_URI)
    version, hashes = register(MlflowClient(TRACKING_URI, TRACKING_URI), args.folder,
                               record=record)
    print(f"Registered {MODEL_NAME} version {version}. It is NOT live yet.")
    for f, h in hashes.items():
        print(f"  {f}  {h}")
    for key, value in record["metrics"].items():
        print(f"  {key}: {value:.4f}")
    print(f"  git commit {record['tags']['git_commit'][:12]}"
          + (" (with uncommitted changes)" if record["tags"]["git_dirty"] == "true" else ""))
    print(f"To make it live: make promote VERSION={version} (on Kubernetes: make k8s-promote)")


if __name__ == "__main__":
    main()