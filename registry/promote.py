"""Make a registered model version live, or roll back, through model-admin-svc (week 4, step 6).

    make promote VERSION=4                      runs the promotion gate first; stops if it says keep
    make promote VERSION=3 OVERRIDE="reason"    promotes even if the gate says keep (recorded)
    make rollback REASON="reason"               back to the version live before the current one

This tool runs on the admin's laptop, the only place the private signing key is. It downloads the
version, checks it loads, runs Manisha's promotion gate against the live model, and asks a person
to confirm. Then it signs the promotion and sends it to POST /v1/model/promote as the `admin`
client. model-admin-svc records who sent it in the audit log, then publishes it to model.events.
Design: docs/mlflow.md
"""

import argparse
import sys
import tempfile
import time
from pathlib import Path

import httpx
import mlflow
from mlflow import MlflowClient

from load.credentials import credentials
from ml.model import FraudModel
from registry.register import TRACKING_URI
from registry.signing import MODEL_NAME, PRIVATE_KEY, model_hashes, model_label, sign_promotion

ALIAS = "production"
ADMIN_CLIENT = "admin"   # deploy/clients.json
ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT / "models" / "fraud-lgbm-v1"   # what scoring serves before any promotion


class GateSaidKeep(Exception):
    """The promotion gate said keep the current model, and no override was given."""


def new_sequence():
    """Milliseconds since 1970: always higher than earlier promotions, with nothing to look up."""
    return int(time.time() * 1000)


def download(version, folder):
    return mlflow.artifacts.download_artifacts(f"models:/{MODEL_NAME}/{version}", dst_path=folder)


class AdminApi:
    """model-admin-svc through Kong, as the admin client."""

    def __init__(self, url):
        self.http = httpx.Client(base_url=url, timeout=30)
        r = self.http.post("/token", json=credentials(ADMIN_CLIENT))
        r.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def _check(self, r):
        if r.status_code != 200:
            detail = r.json().get("detail") if "json" in r.headers.get("content-type", "") else r.text
            raise RuntimeError(f"model-admin-svc refused ({r.status_code}): {detail}")
        return r.json()

    def live(self):
        return self._check(self.http.get("/v1/model"))

    def send(self, action, token):
        return self._check(self.http.post(f"/v1/model/{action}", json={"token": token}))


def run_gate(candidate_folder, current_folder, against):
    """Manisha's promotion gate (ml/eval/promotion_gate.py): candidate vs the live model on the
    candidate's own test days. Needs data/processed/ on this laptop, and takes a few minutes."""
    from ml.eval import promotion_gate as gate  # pandas, scikit-learn: only when promoting

    print(f"promotion gate: candidate vs {against}; loading data and building features...")
    candidate, current = FraudModel.load(candidate_folder), FraudModel.load(current_folder)
    # The candidate's own test days (Manisha, #13): a retrained model is judged on days after
    # its training, and both models must not have seen them (the `unseen` check)
    days = gate.held_out_days(candidate)
    golden = gate.golden_set(gate.load_data(), days)
    # Timed together, interleaved, so a busy moment on this laptop can't fail `fast` (12 Nov)
    cand, curr = gate.measure_pair(candidate, current, golden)
    passed, rows = gate.check(cand, curr)
    rows.append(gate.unseen(days, candidate, current))
    passed = passed and rows[-1]["passed"]
    for r in rows:
        print(f"  {r['check']:7} {r['value']:22} rule {r['rule']:10} "
              f"{'pass' if r['passed'] else 'FAIL'}")
    return {"passed": passed, "against": against,
            "pr_auc_gain": round(cand["PR-AUC"] - curr["PR-AUC"], 4),
            "p95_ms": round(cand["p95 ms"], 2), "flagged_share": round(cand["flagged share"], 4)}


def gate_against_live(api, candidate_folder, download=download, gate=run_gate):
    """Run the gate against whatever is live now: the last promoted version, or the pinned model."""
    live = api.live()["live"]
    if live is None:
        return gate(candidate_folder, PINNED, "the pinned model")
    with tempfile.TemporaryDirectory() as tmp:
        current = download(live["version"], tmp)
        return gate(candidate_folder, current, f"version {live['version']} ({live['label']})")


def confirm_with_human(action, version, hashes, label, gate):
    print(f"About to {action} {MODEL_NAME} version {version} ({label}). Files:")
    for f, h in hashes.items():
        print(f"  {f}  {h}")
    if gate:
        print(f"Promotion gate: {'PASS' if gate['passed'] else 'KEEP (overridden)'}")
    print("Compare these with the values the model's owner gave you.")
    return input("Sign and send? [y/N] ").strip().lower() == "y"


def promote(client, version, private_key, api, action="promote", reason="", override=False,
            download=download, validate=FraudModel.load, gate=gate_against_live,
            confirm=confirm_with_human):
    """Returns model-admin-svc's answer, or None if the person said no. Raises if anything is
    wrong: a version that isn't ready or doesn't load, a gate that says keep without an override,
    or a refusal from model-admin-svc.

    The hashes come from the files just downloaded, then a person checks them: if someone replaced
    the files in the registry, this is the last point to catch it before signing.
    """
    status = client.get_model_version(MODEL_NAME, str(version)).status
    if status != "READY":
        raise RuntimeError(f"version {version} is {status}, not READY")

    with tempfile.TemporaryDirectory() as tmp:
        folder = download(version, tmp)
        validate(folder)   # a model that doesn't load must never be signed
        hashes = model_hashes(folder)
        label = model_label(folder, hashes["model.txt"])
        # A rollback skips the gate on purpose: it is the way back when a model goes wrong
        result = gate(api, folder) if action == "promote" else None
    if result is not None and not result["passed"] and not override:
        raise GateSaidKeep(f"the promotion gate says keep the current model "
                           f"(PR-AUC {result['pr_auc_gain']:+.4f}). To promote anyway, give "
                           "OVERRIDE=\"why\"; the reason goes into the audit log.")
    if not confirm(action, version, hashes, label, result):
        return None

    token = sign_promotion(MODEL_NAME, version, hashes, new_sequence(), private_key,
                           action=action, label=label, reason=reason, gate=result)
    answer = api.send(action, token)
    # After publishing: the message decides what is live, the alias only shows it to people.
    # If this line fails, the model is already live; promoting again repairs the alias.
    client.set_registered_model_alias(MODEL_NAME, ALIAS, str(version))
    return answer


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--version", help="the registered version to make live")
    which.add_argument("--rollback", action="store_true",
                       help="go back to the version live before the current one")
    p.add_argument("--reason", default="", help="why; required for a rollback")
    p.add_argument("--override", default="",
                   help="promote even if the gate says keep; this is the reason recorded")
    p.add_argument("--url", default="http://localhost:8000", help="the gateway (Kong)")
    args = p.parse_args()
    if args.rollback and not args.reason.strip():
        sys.exit("a rollback needs a reason: make rollback REASON=\"why\"")

    mlflow.set_tracking_uri(TRACKING_URI)
    client, api = MlflowClient(TRACKING_URI, TRACKING_URI), AdminApi(args.url)
    action, version = "promote", args.version
    if args.rollback:
        action, version = "rollback", api.live()["previous"]
        if version is None:
            sys.exit("nothing to roll back to: fewer than two promotions are on record")
    reason = (args.override or args.reason).strip()
    try:
        answer = promote(client, version, PRIVATE_KEY.read_text(), api, action=action,
                         reason=reason, override=bool(args.override.strip()))
    except GateSaidKeep as e:
        sys.exit(f"Not promoted: {e}")
    if answer is None:
        print("Cancelled: nothing changed.")
    else:
        print(f"{action}: version {answer['version']} ({answer['label']}) is live, sequence "
              f"{answer['sequence']}, recorded as {answer['by']}. Alias {ALIAS} -> {version}")


if __name__ == "__main__":
    main()
