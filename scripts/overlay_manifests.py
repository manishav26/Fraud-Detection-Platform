"""Kustomize overlays for the config scan (scripts/scan.sh config; docs/supply-chain.md).

An overlay's patch files are fragments: kustomize merges them into the base before anything is
deployed, so a scanner reading one on its own judges settings that never run (a patch without the
base's securityContext looks like a pod with none). So the scan skips the fragments and instead
reads each overlay as kustomize builds it, one file per resource.

    python scripts/overlay_manifests.py overlays             the overlay folders, one per line
    python scripts/overlay_manifests.py patches              their patch files (to skip)
    kustomize build ... | python scripts/overlay_manifests.py split <folder>
                                                             one <Kind>-<name>.yaml per resource
"""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
K8S = ROOT / "deploy" / "k8s"


def overlays():
    """Every folder under deploy/k8s with a kustomization, except the base itself."""
    return sorted(p.parent for p in K8S.glob("*/kustomization.yaml") if p.parent.name != "base")


def patch_files():
    """Repo-relative paths of every patch file an overlay names. Inline patches have no file."""
    found = []
    for folder in overlays():
        spec = yaml.safe_load((folder / "kustomization.yaml").read_text())
        for patch in spec.get("patches") or []:
            if "path" in patch:
                found.append((folder / patch["path"]).resolve().relative_to(ROOT).as_posix())
        for path in spec.get("patchesStrategicMerge") or []:   # the older spelling
            found.append((folder / path).resolve().relative_to(ROOT).as_posix())
    return sorted(found)


def split(text, folder):
    """Write each resource of kustomize's output to <folder>/<Kind>-<name>.yaml. Two resources
    with the same kind and name would overwrite each other, so that stops instead."""
    folder = pathlib.Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for doc in yaml.safe_load_all(text):
        if not doc:
            continue
        name = f"{doc['kind']}-{doc['metadata']['name']}.yaml"
        if name in written:
            raise SystemExit(f"two resources would both be written to {name}")
        (folder / name).write_text(yaml.safe_dump(doc, sort_keys=False))
        written.append(name)
    if not written:
        raise SystemExit("kustomize produced no resources")
    return written


def main(argv):
    if argv == ["overlays"]:
        print("\n".join(p.relative_to(ROOT).as_posix() for p in overlays()))
    elif argv == ["patches"]:
        print("\n".join(patch_files()))
    elif len(argv) == 2 and argv[0] == "split":
        print(f"{len(split(sys.stdin.read(), argv[1]))} resources")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
