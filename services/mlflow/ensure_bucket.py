"""Create the artifact bucket if it is missing. MLflow does not create buckets by itself."""

import os
import time
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

ATTEMPTS = 30   # the object store may still be starting, so retry for about a minute


def main():
    bucket = urlparse(os.environ["MLFLOW_ARTIFACTS_DESTINATION"]).netloc
    s3 = boto3.client("s3", endpoint_url=os.environ["MLFLOW_S3_ENDPOINT_URL"],
                      config=Config(s3={"addressing_style": "path"}))
    for attempt in range(1, ATTEMPTS + 1):
        try:
            existing = [b["Name"] for b in s3.list_buckets()["Buckets"]]
            if bucket not in existing:
                s3.create_bucket(Bucket=bucket)
            print(f"bucket ready: {bucket}", flush=True)
            return
        except (BotoCoreError, ClientError) as e:
            if attempt == ATTEMPTS:
                raise
            print(f"object store not ready (attempt {attempt}): {e}", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
