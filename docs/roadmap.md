# ArcWatch Roadmap

Source: multi-agent brainstorm 2026-05-05 (PM, Security, Perf, SRE, AI, UX).
Three tracks ranked. **Track A is in progress.** B and C are queued.

---

## Track A — Visible Product Leap (IN PROGRESS, started 2026-05-06)

**Goal:** make the product look and feel like a 2026 monitoring platform, not a 2018 admin panel. Unblocks every downstream feature (charts, embedding, integrations).

**Scope:**
1. **Read API v1** — GET endpoints for GPUs, clusters, costs summary, alerts, inference endpoints. Cursor pagination. `read` scope on API keys (already supported by APIKey model).
2. **ECharts dashboards** — server-rendered HTMX fragments on existing templates. Charts: GPU util heatmap, cost trend + month-end forecast band, alert timeline, inference latency p50/p95/p99 histogram, per-model cost treemap.
3. **Demo data + Demo Fleet toggle** — extend existing `seed_demo_data.py` to 30 days of historical hypertable metrics; surface a "Try Demo Fleet" CTA on `no_organization.html` and post-invite empty states.

**Stack decision (locked from brainstorm):** Django + HTMX + Alpine.js + ECharts + Tailwind. No SPA rewrite. SSE deferred to Track A.5 (real-time enhancement) once baseline charts ship.

**Acceptance:** new user lands → toggles Demo Fleet → sees populated charts within 30s. External tool can curl `/api/v1/gpus/` with a read key and get paginated JSON. Charts render server-rendered with HTMX swap-in.

---

## Track B — Trust & Compliance Foundation (QUEUED)

**Goal:** unlock the first enterprise conversation and SOC 2 readiness path.

**Scope (ranked by exploitability × blast radius):**
1. **HMAC-signed agent ingest** — per-endpoint shared secret, `X-ArcWatch-Timestamp` (5-min skew window), Redis nonce cache (10-min TTL) for replay protection. Currently any caller knowing the URL + tenant can spoof metrics.
2. **API rate limiting** — `django-ratelimit` tiered: 60/min read, 10/min write, 5/min per-IP+username login, 100/hour Slack test. Redis backend.
3. **Append-only audit log** — `AuditLog` model (actor, tenant, action, target_type, target_id, before/after JSON, IP, UA, request_id) via Django signals on User, APIKey, AlertRule, Cluster, Endpoint, OrgMembership.
4. **MFA (TOTP)** — optional for all roles, required for admin/owner.
5. **Dependency lockfile + SBOM** — `pip-tools` → `requirements.lock` with hashes (`--require-hashes`). Add `pip-audit` + Trivy in CI; cyclonedx SBOM per release.
6. **Secrets out of `.env`** — SOPS+age (cheap path) or AWS Secrets Manager. KMS-bootstrap the Fernet master key.

**Compliance unlocks:** SOC 2 Type I after 1+3+4+5 land + documented access reviews + encryption-at-rest evidence.

**Estimated effort:** 3 sprints.

---

## Track C — Phase 2 Scale Hardening (QUEUED — partially scoped already in seed_fleet design)

**Goal:** prove the 40k-GPU claim. Without these, ArcWatch chokes between 5k–12k GPUs.

**Scope (ranked by blast radius at 40k GPUs):**
1. **Dashboard SQL aggregation + Redis cache** — `monitor/views/dashboard_views.py:25-75` currently does Python-side `sum()` over `list(qs)`. Push to single `qs.aggregate()` with conditional `Count(Case(When(...)))` for util bands; cache per-org with 15–30s TTL.
2. **Cost snapshot batch writes** — `monitor/services/cost_engine.py:73-118`. Drop per-org `transaction.atomic`; use single `execute_values` of all rows. Pre-compile pricing patterns into a cached dict. Long-term: move to TimescaleDB continuous aggregate joined to a pricing lookup table.
3. **Ingest path: ON CONFLICT + execute_values** — `monitor/services/metric_ingestion.py:53-106`. Replace `update_or_create` per GPU with `bulk_create(update_conflicts=True)` (Django 4.1+). Swap `executemany` → `execute_values`.
4. **Alert engine: group-by-metric** — `monitor/services/alert_engine.py:30-133`. One query per metric type across all orgs (`GROUP BY organization_id`); bulk-load open AlertEvents into a set; cache `get_fleet_cost_rate` per org with 60s TTL.
5. **PgBouncer + connection pool tuning** — `arcwatch/settings.py:91-106`: add `CONN_MAX_AGE=600`, `CONN_HEALTH_CHECKS=True`. Front Postgres with PgBouncer (transaction pooling). Bump gunicorn to `2*CPU+1` workers, `--worker-class gthread --threads 4`.
6. **Write-behind via Redis Streams (architectural shift)** — ingest endpoint XADDs to `metrics:{shard}`, Go consumer drains in 1s windows with `COPY FROM STDIN`. 10–50× ingest headroom; decouples HTTP from DB latency.
7. **TimescaleDB chunk size + compression policy** — switch `gpu_metrics` from default 1d to 6h chunks (40k×30s/day = ~115M rows/chunk is too big). Verify composite index `(gpu_uuid, time)` on cost_snapshots.

**Capacity model breaks at:** Gunicorn 3 workers without pool ~4500 GPUs · `update_or_create` per GPU ~12k GPUs · Cost snapshot Celery single worker ~15k GPUs · Skewed-org dashboard already at 200+ GPUs.

**Pre-req:** `seed_fleet` load harness with balanced (100×50×8) and skewed (1×2500) shapes (already designed in prior session).

**Estimated effort:** 4 sprints.

---

## Deferred / kill list

- On-prem appliance — cede to NVIDIA Base Command, slow procurement.
- Training-run experiment tracking — cede to Weights & Biases.
- Generic APM tracing — cede to Datadog.
- WebSocket dashboards — SSE is sufficient for one-way data flow; saves Channels/ASGI complexity.

---

## Linked artifacts

- Per-request inference tracing (Track A.5+ wedge feature) — see AI brainstorm: `inference_request` Timescale hypertable joining request_id → gpu_uuid → tokens → real $/req. Build after Read API v1 + dashboards land.
- `seed_fleet` load harness design — prior session, balanced + skewed shapes.
