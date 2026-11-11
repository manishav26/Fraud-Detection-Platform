"""The config scan reads overlays as kustomize builds them (scripts/scan.sh config;
docs/supply-chain.md, "Overlays")."""
import importlib.util
import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "overlay_manifests", ROOT / "scripts" / "overlay_manifests.py")
om = importlib.util.module_from_spec(spec)
spec.loader.exec_module(om)

# The kinds each waived finding is about
FINDING_KINDS = {"KSV-0014": {"Deployment", "StatefulSet"}, "KSV-0047": {"ClusterRole", "Role"}}


def test_every_overlay_patch_is_found_and_exists():
    patches = om.patch_files()
    assert "deploy/k8s/vm/cassandra-3-nodes.yaml" in patches
    for path in patches:
        assert (ROOT / path).is_file(), path


def test_the_base_is_not_an_overlay():
    assert [p.name for p in om.overlays()] == ["vm"]


def test_split_writes_one_file_per_resource(tmp_path):
    text = "kind: Service\nmetadata: {name: a}\n---\nkind: StatefulSet\nmetadata: {name: a}\n"
    assert om.split(text, tmp_path) == ["Service-a.yaml", "StatefulSet-a.yaml"]
    assert yaml.safe_load((tmp_path / "StatefulSet-a.yaml").read_text())["kind"] == "StatefulSet"


def test_split_refuses_two_resources_with_one_file_name(tmp_path):
    text = "kind: Service\nmetadata: {name: a}\n---\nkind: Service\nmetadata: {name: a}\n"
    with pytest.raises(SystemExit, match="Service-a.yaml"):
        om.split(text, tmp_path)


def test_split_refuses_empty_output(tmp_path):
    with pytest.raises(SystemExit, match="no resources"):
        om.split("", tmp_path)


def test_merged_waivers_cover_exactly_the_base_waivers_resources():
    """A merged-file waiver exists only for a resource whose base file has the same waiver, in
    every overlay: a waiver can't quietly grow to cover something the base doesn't waive."""
    waivers = yaml.safe_load((ROOT / ".trivyignore.yaml").read_text())["misconfigurations"]
    for waiver in waivers:
        kinds = FINDING_KINDS[waiver["id"]]
        base = [p for p in waiver["paths"] if p.startswith("deploy/k8s/base/")]
        expected = set()
        for path in base:
            for doc in yaml.safe_load_all((ROOT / path).read_text()):
                if doc and doc["kind"] in kinds:
                    for overlay in om.overlays():
                        expected.add(f"merged/{overlay.name}/{doc['kind']}-{doc['metadata']['name']}.yaml")
        merged = {p for p in waiver["paths"] if p.startswith("merged/")}
        assert merged == expected, waiver["id"]
        assert set(waiver["paths"]) == set(base) | merged, waiver["id"]


def test_scan_skips_patches_and_reads_overlays_merged():
    scan = (ROOT / "scripts" / "scan.sh").read_text()
    assert "OVERLAY_ARGS=(patches)" in scan and '--skip-files "$patch"' in scan
    assert '"$KUSTOMIZE" build "/repo/$overlay"' in scan
    assert "registry.k8s.io/kustomize/kustomize:v5.8.1" in scan
