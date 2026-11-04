# Who may call what (week 3, step 6)

Threats 1, 2 and 7; tests ST1, ST2 and ST7 (`security/threat-model.md`).

## Clients and tokens

The clients and their roles are listed in `deploy/clients.json` (in git, so changes are reviewed).
`make keys` generates a random secret for each one into `keys/` (not in git):

| File | Holds | Used by |
|---|---|---|
| `keys/clients.json` | plain secrets | tools on the laptop only: `make token`, replay, load |
| `keys/clients.issuer.json` | SHA-256 of each secret | the token issuer, which never sees a plain secret |
| `deploy/kong/kong.yml` | one consumer per client, with its role as its group | Kong |

**Getting a token:** `POST /token` with `{"client_id": ..., "client_secret": ...}`. A wrong secret
and an unknown client get the same 401, and the secret is compared in constant time, so neither the
answer nor its timing hints at which was wrong. From the laptop: `TOKEN=$(make -s token CLIENT=ana)`.

**The token:** `iss` and `sub` are the client id, plus `role`, and it **expires after 15 minutes**,
which limits what a stolen token is worth. Signed RS256 with the issuer's private key.

**How Kong knows who it is:** Kong's free edition can't read custom claims, but it can map a
token's `iss` to a consumer. So each client is a Kong consumer whose key is its id, all checked with
the issuer's public key. Kong then adds `X-Consumer-Username` (the client id) for the services, so
a resolved case now names the analyst who resolved it.

**Hashes, not slow password hashing:** the secrets are 32 random bytes, which can't be guessed, so a
plain SHA-256 is enough. Slow hashing (bcrypt, scrypt) is for passwords people choose.

## Dev clients

| Client | Role | For |
|---|---|---|
| `merchant-1`, `merchant-2` | client | sending payments. Two, so ST7 can flood with one and measure the other |
| `load-test` | client | replay and load tests. Will get a higher rate limit, as our tools send about 100/s |
| `ana`, `ben` | analyst | working on cases |
| `admin` | admin | reading cases; promoting models still needs the signing key on the laptop |

## Roles table (ST2): enforced from the next commit

| Route | client | analyst | admin |
|---|---|---|---|
| `POST /v1/transactions/score` | yes | 403 | 403 |
| `GET /cases`, `GET /cases/{id}` | 403 | yes | yes |
| `POST /cases/{id}/resolve` | 403 | yes | 403 |
| `POST /token` | needs a client id and secret | | |
