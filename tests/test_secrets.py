"""The cluster's passwords stay encrypted (week 4, step 5 part B; docs/secrets.md).

No dev password is written anywhere the cluster reads; every password k8s-up needs is in the
encrypted file, and that file holds only encrypted values. Compose keeps its dev passwords.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ENC = ROOT / "deploy/k8s/secrets.enc.env"
HELPER = (ROOT / "scripts/k8s_secrets.sh").read_text()
NAMES = re.findall(r"^\s+([A-Z_]+)\s+#", HELPER.split("SECRET_NAMES=(")[1].split("\n)")[0], re.MULTILINE)
DEV = re.compile(r"\w+_dev_only")


def k8s_recipes():
    """The Makefile's k8s-* targets with their commands."""
    text = (ROOT / "Makefile").read_text()
    return "\n".join(m.group(0) for m in re.finditer(r"^k8s-[\w-]+:.*\n(\t.*\n)*", text, re.MULTILINE))


def test_the_list_of_passwords_was_read():
    assert len(NAMES) == 18 and {"POSTGRES_PASSWORD", "SIEM_TOKEN", "CASSANDRA_FEATURES_PASSWORD",
                                 "REDPANDA_ADMIN_PASSWORD", "REDPANDA_TOOLS_PASSWORD"} <= set(NAMES)


@pytest.mark.parametrize("path", ["scripts/k8s_up.sh", "scripts/k8s_secrets.sh",
                                  "scripts/gen_secrets.sh", *[str(p.relative_to(ROOT))
                                  for p in sorted((ROOT / "deploy/k8s").rglob("*.yaml"))]])
def test_nothing_the_cluster_reads_has_a_dev_password(path):
    assert not DEV.findall((ROOT / path).read_text())


def test_the_cluster_make_targets_have_no_dev_password():
    assert not DEV.findall(k8s_recipes())


def test_laptop_tools_get_the_cluster_passwords_from_sops():
    recipes = k8s_recipes()
    for target in ("k8s-backfill", "k8s-verify-audit", "k8s-read-timing", "k8s-st4-db"):
        recipe = re.search(rf"^{target}:.*\n\t(.*)", recipes, re.MULTILINE).group(1)
        assert recipe.startswith("./scripts/k8s_secrets.sh run"), target


@pytest.mark.parametrize("name", NAMES)
def test_k8s_up_uses_every_password(name):
    # k8s_up.sh, or scripts/kafka_security.sh which it runs (the Redpanda users, by name)
    up = (ROOT / "scripts/k8s_up.sh").read_text()
    assert f"${name}" in up or f":{name}" in (ROOT / "scripts/kafka_security.sh").read_text()


def test_k8s_up_sets_every_postgres_login():
    up = (ROOT / "scripts/k8s_up.sh").read_text()
    for role in ("fraud", "mlflow", "alerts", "model_admin"):
        assert f"ALTER ROLE {role} PASSWORD" in up


def test_sops_encrypts_the_file_for_an_age_key():
    rules = yaml.safe_load((ROOT / ".sops.yaml").read_text())["creation_rules"]
    rule = next(r for r in rules if re.search(r["path_regex"], "deploy/k8s/secrets.enc.env"))
    assert rule["age"].startswith("age1") and "AGE-SECRET-KEY" not in (ROOT / ".sops.yaml").read_text()


def test_a_decrypted_copy_would_be_ignored_by_git():
    for path in ("deploy/k8s/secrets.env", "deploy/k8s/plain.env", "keys.txt"):
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False).returncode == 0, path
    assert subprocess.run(["git", "check-ignore", "-q", "deploy/k8s/secrets.enc.env"], cwd=ROOT, check=False).returncode == 1


@pytest.mark.skipif(not ENC.exists(), reason="no encrypted file yet: make k8s-secrets")
def test_the_encrypted_file_has_every_password_and_only_encrypted_values():
    values = dict(line.split("=", 1) for line in ENC.read_text().splitlines() if "=" in line)
    passwords = {k: v for k, v in values.items() if not k.startswith("sops_")}
    assert set(passwords) == set(NAMES)
    assert all(v.startswith("ENC[AES256_GCM,data:") for v in passwords.values())
    assert any(k.startswith("sops_age__") for k in values) and "sops_mac" in values


@pytest.mark.skipif(not (shutil.which("sops") and shutil.which("age-keygen")),
                    reason="needs sops and age (brew install sops age)")
def test_gen_secrets_round_trip(tmp_path):
    """With a throwaway key: makes all, keeps them on a rerun, makes new ones on ROTATE=1."""
    subprocess.run(["age-keygen", "-o", tmp_path / "key.txt"], check=True, capture_output=True)
    public = re.search(r"age1\w+", (tmp_path / "key.txt").read_text()).group(0)
    (tmp_path / "sops.yaml").write_text(f"creation_rules:\n  - age: {public}\n")
    env = {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin", "HOME": str(tmp_path),
           "SOPS_AGE_KEY_FILE": str(tmp_path / "key.txt"), "SOPS_CONFIG": str(tmp_path / "sops.yaml"),
           "SECRETS_FILE": str(tmp_path / "s.enc.env")}

    def run(*args, **extra):
        return subprocess.run(args, cwd=ROOT, env={**env, **extra}, check=True,
                              capture_output=True, text=True).stdout

    def show():
        return run("scripts/k8s_secrets.sh", "show", "POSTGRES_PASSWORD").strip()

    run("scripts/gen_secrets.sh")
    first = show()
    assert re.fullmatch(r"[0-9a-f]{48}", first) and first not in (tmp_path / "s.enc.env").read_text()
    assert "nothing changed" in run("scripts/gen_secrets.sh") and show() == first
    run("scripts/gen_secrets.sh", ROTATE="1")
    assert show() != first
