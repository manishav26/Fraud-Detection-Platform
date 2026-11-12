"""What a model folder was trained with, for the MLflow registry.

`make register` logs this on the model's MLflow run, so every registered version says:
  params   the LightGBM settings read from model.txt itself, the data windows and thresholds
           from meta.json
  metrics  PR-AUC, recall @ 1% FPR, flagged share and score + reasons p95 on the model's own
           test days (92-109 for v1)
  tags     git commit (and whether the working copy had changes), LightGBM version, file hashes

The settings come from the model file, not from ml/configs/lgbm.yaml, so they describe the
model that was actually built even if the config has changed since.

    python -m ml.train.training_record models/fraud-lgbm-v1      # print the record
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np

LGBM_SETTINGS = {                     # LightGBM's name in model.txt -> name in MLflow
    "num_iterations": "n_estimators",
    "learning_rate": "learning_rate",
    "num_leaves": "num_leaves",
    "min_data_in_leaf": "min_child_samples",
    "bagging_fraction": "subsample",
    "bagging_freq": "subsample_freq",
    "feature_fraction": "colsample_bytree",
    "lambda_l2": "reg_lambda",
    "seed": "random_state",
    "deterministic": "deterministic",
}
ROOT = Path(__file__).resolve().parents[2]


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git_commit(root=ROOT):
    """(commit, dirty) of the working copy, or ("unknown", False) outside git."""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                text=True, check=True).stdout.strip()
        changes = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                                 capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown", False
    return commit, bool(changes)


def lgbm_settings(model_file):
    """The training settings LightGBM saved inside model.txt."""
    import lightgbm as lgb

    saved = lgb.Booster(model_file=str(model_file)).params
    return {ours: saved[theirs] for theirs, ours in LGBM_SETTINGS.items() if theirs in saved}


def golden_metrics(folder):
    """Model quality and speed on the model's own test days. Loads the data: ~1-2 min."""
    from sklearn.metrics import roc_curve

    from ml.eval.promotion_gate import golden_set, held_out_days, measure
    from ml.model import FraudModel
    from ml.train.calibrate import TARGET, load_data

    model = FraudModel.load(folder)
    golden = golden_set(load_data(), held_out_days(model))   # the model's own test days
    measured = measure(model, golden)
    probs = model.predict_rows(golden[model.features].to_dict("records"))
    fpr, tpr, _ = roc_curve(golden[TARGET], probs)
    return {"test_pr_auc": measured["PR-AUC"],
            "test_recall_at_1pct_fpr": float(np.interp(0.01, fpr, tpr)),
            "test_flagged_share": measured["flagged share"],
            "score_reasons_p95_ms": measured["p95 ms"]}


def training_record(folder, with_metrics=True):
    """{"params": ..., "metrics": ..., "tags": ...} for one model folder."""
    folder = Path(folder)
    meta = json.loads((folder / "meta.json").read_text())
    info, thresholds = meta.get("info", {}), meta.get("thresholds", {})
    params = {**lgbm_settings(folder / "model.txt"),
              **{k: info[k] for k in ["train_days", "calibration_days", "test_days",
                                      "n_features"] if k in info},
              **thresholds}
    commit, dirty = git_commit()
    model_hash = _sha256(folder / "model.txt")
    tags = {"git_commit": commit, "git_dirty": str(dirty).lower(),
            "lightgbm": str(info.get("lightgbm", "unknown")),
            "model_txt_sha256": model_hash, "meta_json_sha256": _sha256(folder / "meta.json"),
            "model_label": f"{info.get('name', folder.name)}-{model_hash[:8]}"}
    metrics = golden_metrics(folder) if with_metrics else {}
    return {"params": params, "metrics": metrics, "tags": tags}


def log_record(client, run_id, record):
    """Write a training record onto an MLflow run."""
    for key, value in record["params"].items():
        client.log_param(run_id, key, str(value))
    for key, value in record["metrics"].items():
        client.log_metric(run_id, key, float(value))
    for key, value in record["tags"].items():
        client.set_tag(run_id, key, value)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("folder")
    p.add_argument("--no-metrics", action="store_true", help="skip the 1-2 min evaluation")
    args = p.parse_args()
    print(json.dumps(training_record(args.folder, not args.no_metrics), indent=2))


if __name__ == "__main__":
    main()