# Multi-tenancy runbook — Secure Boxes (Epic C, story C1)

One image, N silos (design §10, §13.3, §14). Each tenant is one
docker-compose stack — frontend (nginx) + backend (FastAPI) +
Postgres (dedicated container) — with its own env file, its own
volume, and its own Docker network, on one EC2 host to start. Boxes
share code, never data. The only shared data in the design is the
catalog, which arrives with C2; a C1 box is fully independent.

## 1. Add a new tenant box

```bash
# From the repo root. The slug is lowercase letters/digits/hyphens.
python3 scripts/provision_tenant_box.py --slug na \
    --tenant-name "North America" --market NA \
    --frontend-port 5174 --backend-port 8001
# RECORD the printed seed password in the tenant's secret store now.
# The script prints it once and stores it nowhere else.

docker compose -f docker-compose.tenant.yml -p vap-na \
    --env-file boxes/na.env up -d --build
```

Port rules: every box needs host ports no other box uses (frontend
and backend). The database publishes NO port — Postgres is reachable
only from the box's own backend over the box network. Never set
`container_name` in the template: the project name (`-p vap-<slug>`)
prefixes containers, the volume (`vap-<slug>_db`), and the network
(`vap-<slug>_net`), and that prefixing is what keeps boxes apart.

Verify the new box, without touching any other box:

```bash
# Health reports the box's own slug + database fingerprint (no secrets).
curl http://localhost:5174/api/health
# Expect: "box": {"tenant_slug": "na", "database": "postgres:voice_platform_na", ...}
# Log in with the recorded seed password and confirm the box holds
# only its own tenant (its own campaigns, leads, calls).
```

## 2. Reverse proxy — subdomain routing (needs a domain)

Until a custom domain exists, boxes are reached directly by IP:port
(frontend port). The proxy config is still generated and validated
now, so the subdomain cutover is a config swap, not new work:

```bash
# tenants.json: {"<subdomain>": <frontend host port>, ...}
echo '{"qgbs.example.com": 5173, "na.example.com": 5174}' > tenants.json
python3 scripts/generate_proxy_config.py tenants.json --out /etc/nginx/conf.d/tenants.conf
# Once the domain exists and certbot has issued certificates:
python3 scripts/generate_proxy_config.py tenants.json --tls --out /etc/nginx/conf.d/tenants.conf
nginx -t && systemctl reload nginx
```

Rules the generator enforces: one subdomain routes to exactly one
box's frontend port; two subdomains never share a port; unknown
hosts hit the default server and get 444 (closed, no tenant served).
The proxy is the only public front door: it listens on 80 (and 443
with TLS); box backend ports stay bound to 127.0.0.1.

## 3. Per-tenant backup and restore (never cross-tenant)

```bash
# Backup ONE box (its own container, its own database — nothing else):
docker compose -f docker-compose.tenant.yml -p vap-na \
    --env-file boxes/na.env exec -T db \
    pg_dump -U vap_na voice_platform_na > na-backup.sql

# Restore to THAT box only (stops at that box; other boxes keep running):
docker compose -f docker-compose.tenant.yml -p vap-na \
    --env-file boxes/na.env exec -T db \
    psql -U vap_na -d voice_platform_na < na-backup.sql
```

Restoring one tenant's dump cannot touch another tenant: the dump
names one database, the command names one container, and every box
has its own volume. Keep dumps versioned per tenant (S3 versioning
is the production target, §14.2).

## 4. Restart / rebuild one box (isolation rule)

```bash
# Safe: data lives in the box volume, which restart/rebuild keep.
docker compose -f docker-compose.tenant.yml -p vap-na \
    --env-file boxes/na.env restart
docker compose -f docker-compose.tenant.yml -p vap-na \
    --env-file boxes/na.env up -d --build

# FORBIDDEN on any box: `docker compose down -v` deletes the box's
# database volume. `down` without -v keeps the data.
```

A box restart/rebuild never touches another box: separate
containers, separate volumes, separate networks, separate env files.

## 5. Administration

- No internet SSH to tenant boxes. Administration is via AWS SSM
  Session Manager (design §14.1); no public database or backend
  ports, ever. (The demo's cloudflared tunnel is demo-only and is
  not part of production.)
- Secrets: per-box env files with mode 0600 are acceptable to
  start; AWS Secrets Manager is the target (§14.2). Never commit
  `boxes/*.env` — `.gitignore` excludes the directory, and the
  provision script refuses to overwrite an existing box env file
  without `--force`.
- Logs: per-tenant log groups, no credentials, no PII — fingerprints
  only (the health endpoint's database fingerprint is the pattern).

## 6. What C1 does NOT cover (later stories)

- C2: the catalog + adoption (shared agent versions, version
  metadata, pinned campaigns). A C1 box's agents live in its own
  database only.
- C3: per-tenant telephony mode (a C1 box still reads the
  TELEPHONY_MODE env value; one mode per box, never both).
- C4: per-tenant webhooks/secrets/JWT/subdomain routing enforcement
  in code (the proxy template is routing-ready; per-tenant webhook
  secrets are a C4 build item).
- C5: roles (Super Admin / Tenant Admin / Team Leader / Caller).
- C6: any-call monitoring. C7: QGBS migration as tenant #1.
