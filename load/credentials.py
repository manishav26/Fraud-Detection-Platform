"""Client secrets for tools on the laptop, from keys/clients.json (made by `make keys`).

Print a token for a client:   make -s token CLIENT=ana
    TOKEN=$(make -s token CLIENT=merchant-1)
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

CLIENTS = Path(__file__).resolve().parents[1] / "keys" / "clients.json"
LOAD_CLIENT = "load-test"   # replay and load tests send as this client (docs/access.md)


def credentials(client_id, path=CLIENTS):
    clients = json.loads(Path(path).read_text())
    if client_id not in clients:
        sys.exit(f"unknown client {client_id!r}; known: {', '.join(clients)}")
    return {"client_id": client_id, "client_secret": clients[client_id]["secret"]}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("client")
    p.add_argument("--url", default="http://localhost:8000")
    args = p.parse_args()
    r = httpx.post(f"{args.url}/token", json=credentials(args.client), timeout=10)
    if r.status_code != 200:
        sys.exit(f"{r.status_code}: {r.text}")
    print(r.json()["access_token"])


if __name__ == "__main__":
    main()
