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

## Roles table (ST2)

| Route | client | analyst | admin | no token |
|---|---|---|---|---|
| `POST /v1/transactions/score` | yes | 403 | 403 | 401 |
| `GET /cases`, `GET /cases/{id}` | 403 | yes | yes | 401 |
| `POST /cases/{id}/resolve` | 403 | yes | 403 | 401 |
| `POST /token` | needs a client id and secret | | | |

**Enforced twice:**
1. **Kong** (`deploy/kong/kong.tpl.yml`): on each route, `jwt` checks the token and finds the client,
   then `acl` lets only the listed roles through. The case routes are split into a GET route and a
   POST route, because reading and resolving allow different roles. Forbidden requests stop here.
2. **alert-svc** checks the role again, from the `X-Consumer-Groups` header Kong adds. Kong replaces
   any such header a client sends, so it can't be faked through the gateway. This second check
   matters until week 4's NetworkPolicies stop anyone reaching alert-svc without Kong.

**Test:** `make st2` calls every route as every role and as no one. For the case routes it uses a case
id that doesn't exist: allowed roles get past both checks and get 404, forbidden ones get 403 from
Kong. So it checks access without resolving a real case, and can run any time.
