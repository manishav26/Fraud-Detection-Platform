"""Model registry tools. Design: docs/mlflow.md"""

import os

# Always move model files through the MLflow server. MLflow's client otherwise asks for a direct
# link to the object store (`objectstore:9000`), a name that only exists inside Docker, and retries
# for many minutes on a laptop. This can only be set on the client: the server has no such switch.
os.environ["MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD"] = "false"
os.environ["MLFLOW_ENABLE_PROXY_MULTIPART_UPLOAD"] = "false"
