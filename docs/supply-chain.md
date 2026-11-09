# Supply chain and image hardening (week 4, step 5)

Threat 13: a vulnerable or malicious dependency, or a secret committed by mistake. Control: four
scans on every push, the same ones on the laptop. Test: ST10 (CI green, no unwaived finding).

`make scan` (or `scripts/scan.sh secrets|deps|config|images`) and the `scan` job in
`.github/workflows/ci.yml` run the same script, with the same pinned tools in Docker images.

| Scan | Tool (pinned) | What it reads | Fails on |
|---|---|---|---|
| secrets | gitleaks v8.30.1 | every commit in the history | any secret it recognises |
| deps | pip-audit 2.10.1 (in `python:3.11-slim`) | the versions pinned in `requirements-dev.txt` and each service's `requirements.txt` | any known vulnerability |
| config | Trivy 0.76.1 | Dockerfiles, Kubernetes YAML, compose | HIGH or CRITICAL settings |
| images | Trivy 0.76.1 | our 6 built images: OS packages and every Python package actually installed | HIGH or CRITICAL with a fix available |

- **pip-audit checks exactly our pins** (`--no-deps`). What those packages pull in is checked
  where it really lives: in the images, by Trivy.
- **Only fixable image findings fail the build** (`--ignore-unfixed`). One with no fix released
  can't be acted on; it shows in the report but doesn't block.
- **Waivers are named, per file, with a reason:** `.trivyignore.yaml`, `.gitleaks.toml` (none
  needed so far). A waiver covers only the files it lists.

## What the first scan found, and what changed

| Finding | Where | Change |
|---|---|---|
| gitleaks | 80 commits | nothing found |
| pip-audit | 7 requirements files | nothing found |
| zlib1g CVE-2026-76844, CVE-2026-85091 (HIGH) | all 6 images, from the base image | `apt-get upgrade` in each Dockerfile: Debian's fixed build |
| wheel 0.45.1 CVE-2026-24049, jaraco.context 5.3.0 CVE-2026-23949 (HIGH) | all 6 images: copies bundled inside the base image's setuptools 79 | setuptools 84.2.1 and wheel 0.48.0 in each Dockerfile |
| DS-0002: runs as root (HIGH) | our 5 service images | a user without root (uid 10001); MLflow already had one |
| KSV-0118: no pod security settings (HIGH) | all 13 workloads | every pod uses the runtime's default seccomp filter; our 5 also must not run as root |
| KSV-0014: writable root filesystem (HIGH) | all 13 workloads | our 5: read-only, with a small writable `/tmp` (scoring downloads promoted models there). The 8 third-party images: waived (they write their own config and runtime files) |
| KSV-0047: Prometheus may use the nodes' proxy (HIGH) | Prometheus | waived: that's how it reads cAdvisor's CPU numbers, read-only |

After the changes, all four scans pass. The token issuer's key is also mounted readable by its
own group only (`defaultMode: 0440`, `fsGroup: 10001`) instead of everyone in the pod.

`tests/test_k8s_manifests.py` checks that our services stay non-root and read-only, every pod
keeps the seccomp filter, and every image keeps the security updates.

## Limits

- **Base image updates happen at build time** (`apt-get upgrade`), so two builds a week apart can
  differ. Pinning the base by digest would make builds repeatable but needs a regular bump; for
  this project, fresh fixes matter more.
- **The 8 third-party images keep writable filesystems** (waived above).
- **`rustfs/rustfs:latest`** is the one image without a version tag; Trivy reports it below HIGH.
  Pinning it is a small follow-up.
- The cluster's passwords are covered separately: random, and in git only encrypted with SOPS
  and age (part B, `docs/secrets.md`).
