"""ST9 attack: change a registered model's file directly in the object store, behind MLflow.

Runs inside the MLflow container, which has the storage password: make st9-tamper VERSION=2
"""

import os
import sys
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from mlflow import MlflowClient


def main():
    version = sys.argv[1]
    source = MlflowClient("http://localhost:5000").get_model_version("fraud-lgbm", version).source
    key = f"{urlparse(source).path.lstrip('/')}/model.txt"
    bucket = urlparse(os.environ["MLFLOW_ARTIFACTS_DESTINATION"]).netloc
    s3 = boto3.client("s3", endpoint_url=os.environ["MLFLOW_S3_ENDPOINT_URL"],
                      config=Config(s3={"addressing_style": "path"}))
    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    s3.put_object(Bucket=bucket, Key=key, Body=data + b"\n")
    print(f"changed s3://{bucket}/{key}: one extra byte")


if __name__ == "__main__":
    main()
