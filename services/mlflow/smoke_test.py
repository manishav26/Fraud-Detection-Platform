"""Proves the registry works end to end, without a real model. Run: make mlflow-check

Registers a small test file as a model version, points the `production` alias at it, downloads
it through the alias and compares the bytes. Uses Postgres (records) and the object store (files).
"""

import tempfile
from pathlib import Path

import mlflow
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

NAME, ALIAS = "smoke-test", "production"
CONTENT = "not a real model\n"


def main():
    mlflow.set_tracking_uri("http://localhost:5000")
    client = MlflowClient()
    try:
        client.delete_registered_model(NAME)   # left over from an earlier run
    except MlflowException:
        pass

    experiment = client.get_experiment_by_name(NAME)
    experiment_id = experiment.experiment_id if experiment else client.create_experiment(NAME)
    run_id = client.create_run(experiment_id).info.run_id

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "model.txt"
        path.write_text(CONTENT)
        client.log_artifact(run_id, str(path), "model")
    client.set_terminated(run_id)
    print("1. uploaded a test file to the object store")

    client.create_registered_model(NAME)
    source = f"{client.get_run(run_id).info.artifact_uri}/model"
    version = client.create_model_version(NAME, source, run_id=run_id).version
    client.set_registered_model_alias(NAME, ALIAS, version)
    print(f"2. registered {NAME} version {version}, alias {ALIAS} points to it")

    with tempfile.TemporaryDirectory() as tmp:
        folder = mlflow.artifacts.download_artifacts(f"models:/{NAME}@{ALIAS}", dst_path=tmp)
        assert (Path(folder) / "model.txt").read_text() == CONTENT, "downloaded file differs"
    print("3. downloaded it through the alias: bytes match")

    client.delete_registered_model(NAME)
    client.delete_run(run_id)
    print("OK: registry, Postgres and object store all work")


if __name__ == "__main__":
    main()
