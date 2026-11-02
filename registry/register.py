"""Add a trained model folder to the MLflow registry as a new version.

    make register MODEL_FOLDER=models/fraud-lgbm-v1

Registering does not make a model live: that takes `make promote`, and the promotion key.
"""

import argparse
import os

import mlflow
from mlflow import MlflowClient

from ml.model import FraudModel
from registry.signing import MODEL_NAME, model_hashes

TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5050")


def register(client, folder, validate=FraudModel.load):
    """Returns (version, hashes). A folder that doesn't load is refused before any upload."""
    validate(folder)
    hashes = model_hashes(folder)

    experiment = client.get_experiment_by_name(MODEL_NAME)
    experiment_id = experiment.experiment_id if experiment else client.create_experiment(MODEL_NAME)
    run_id = client.create_run(experiment_id).info.run_id
    client.log_artifacts(run_id, str(folder), "model")
    client.set_terminated(run_id)

    if not client.search_registered_models(f"name='{MODEL_NAME}'"):
        client.create_registered_model(MODEL_NAME)
    source = f"{client.get_run(run_id).info.artifact_uri}/model"
    version = client.create_model_version(MODEL_NAME, source, run_id=run_id).version
    return version, hashes


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--folder", required=True)
    args = p.parse_args()
    mlflow.set_tracking_uri(TRACKING_URI)
    version, hashes = register(MlflowClient(TRACKING_URI, TRACKING_URI), args.folder)
    print(f"Registered {MODEL_NAME} version {version}. It is NOT live yet.")
    for f, h in hashes.items():
        print(f"  {f}  {h}")
    print(f"To make it live: make promote VERSION={version}")


if __name__ == "__main__":
    main()
