# MeshMoe Server

Production control-plane: node scheduling, honor (Joule) settlement.

| Service | Port | Role |
|---|---|---|
| `router_server.py` | 4001 | Node registry, task queue (poll/result), expert-aware scheduling |
| `credits_server.py` | 4005 | Accounts, node_token issuance, append-only Joule ledger |

PostgreSQL (schema migration): `schema_v3_honor.sql` — idempotent, run once per environment:

```bash
psql -h 127.0.0.1 -U <user> -d meshmoe -f schema_v3_honor.sql
```

## Environment variables

| Var | Used by | Purpose |
|---|---|---|
| `MESHMOE_INTERNAL_KEY` | both | Platform-level internal auth (router↔credits). Never ship a default to prod. |
| `MESHMOE_CREDITS_URL` | router, edge nodes | Credits base URL (default `http://127.0.0.1:4005`) |
| `PG_HOST/PG_USER/PG_PASSWORD` | credits | Database credentials via env, never hardcoded |

## Token & honor flow (v3)

1. Edge node boots with no token → `POST /register_node` (anonymous) → receives
   deterministic `node_token` (HMAC of node_id), stores sha256 hash only.
2. Router verifies `X-Node-Token` on `/task/poll` and `/task/result`.
3. Task results carry `joules_est`; router forwards to credits `POST /report_joules`,
   which clamps against `GPU watts × duration × 1.5` before appending to
   `joule_ledger` (append-only, non-consumable).
4. Owner binding: `POST /claim_node` links an anonymous node to a user account;
   all historical ledger entries follow the owner.

Node-facing endpoints (`/register_node`, `/claim_node`, `/report_joules`) skip
`X-Internal-Key` auth — they authenticate via `node_token` itself.
