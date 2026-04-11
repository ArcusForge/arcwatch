# A0 — Stabilize In-Flight Work

- **Spec ID:** A0
- **Status:** Approved, ready for implementation planning
- **Date:** 2026-04-10
- **Author:** Brainstorming session (Claude Code + @zeusofyork)
- **Supersedes:** none
- **Part of sequence:** A0 → A1 → B' → E' → C → D (full ArcWatch refactor program)

---

## 1. Purpose

Convert the 22-file in-flight batch on `master` into a safe, committed, production-ready foundation. Fix real bugs, finish half-started refactors, close tenant-isolation gaps, then stop. A0 is surgical: no feature work, no large-file splits, no performance tuning beyond what is already in flight.

A0 is the prerequisite for A1 (code-health refactor) and every downstream spec. Everything below assumes A0 has landed first.

## 2. Background

ArcWatch (`arcwatch.arcusautomate.com`) is a multi-tenant Django + Go platform for GPU fleet and LLM cost monitoring, deployed on AWS EC2 behind nginx with TimescaleDB + Redis + Celery. At the time of this spec, 22 files have uncommitted modifications representing a mix of async-safety migration, transaction-safety tightening, TimescaleDB retention/compression, input validation, and ops hardening.

A survey of the diff surfaced seven red flags and two incomplete refactors. A0 closes those and commits the batch.

### 2.1 In-flight work already complete (not repeated in A0)

The following are already done in the uncommitted diff and do **not** need re-implementation:

- `threading.local()` → `contextvars.ContextVar` async-safe tenant scoping in `monitor/models/base.py`
- `TenantMiddleware` wired into `arcwatch/settings.py` MIDDLEWARE list
- Multi-tenant view enforcement across `alert_views`, `cost_views`, `dashboard_views`, `inference_views`, `llm_views` (reading org from `request.user.profile.organization`)
- TimescaleDB retention policies and compression in migration `0012_perf_indexes_and_retention`
- Query optimization in `services/alert_engine.py` (ORM `F()` / `ExpressionWrapper` replacing Python-level loops)
- Per-org chunked iteration in `services/cost_engine.py` (`.iterator(chunk_size=500)`)
- `transaction.atomic()` wrapping on metric and inference ingestion services
- Input validation on Slack webhook URLs, REST API payloads, password validation, username collision detection
- Production security headers: HSTS, SSL redirect, secure cookies, 8-hour session timeout
- Health check endpoints `/api/health/` (liveness) and `/api/ready/` (readiness)
- Real `nvidia-smi` parsing in `agent/internal/collector/gpu.go`
- Deterministic hashing (`hashlib.md5`) replacing Python `hash()` for endpoint IDs
- `save_user_profile` post-save signal converted to no-op (confirmed correct: prevents recursion loop; profile persistence happens explicitly via `.profile.save()` in views)

### 2.2 Red flags A0 must fix

1. **`cost_engine.py:161`** — `COUNT(*) * {INTERVAL_SECONDS} / 3600.0` still uses f-string interpolation. Not exploitable (constant), but inconsistent with the rest of the parameterized SQL in the module.
2. **`middleware.py:24`** — `TenantMiddleware` swallows all exceptions when resolving `request.user.profile.organization`. Silent failure leaves `_current_org` unset, allowing cross-org reads. **Tenant-isolation leak.**
3. **`cost_engine.compute_cost_snapshot()`** — Per-org loop at `cost_engine.py:54-101` is not wrapped in `transaction.atomic()`. A crash mid-insert leaves a partial snapshot for an org.
4. **`health_views.py`** — `/api/health/` and `/api/ready/` are added to `urls.py`, but under the new `IsAuthenticated` DRF default they will 401. (Addressed indirectly via the middleware allowlist in §4.1.)
5. **`forms.py` (AlertRuleForm)** — Slack webhook regex validator exists on the `AlertRule` model field but not on the form. Form errors surface at the model layer with an ugly message instead of at the form layer with a user-friendly one.
6. **`api_auth.py`** — Cosmetic change from `HTTP_X_API_KEY` lookup to `request.headers.get("X-Api-Key")`. Functionally equivalent (Django normalizes headers), but undocumented in any changelog. **Downgraded from bug to operational-awareness note.**
7. **`settings_views.py`** — `reverse()` redirect migration is only partial; some hardcoded paths remain.

### 2.3 Permission gaps surfaced by audit

Research confirmed three endpoints with gaps against the tiered RBAC policy (§5):

- `settings_views.py:55` `settings_api_keys` — uses inline `_is_admin()` check only, no `@require_admin` decorator
- `settings_views.py:110` `create_alert_rule` — gated at `@require_admin`; should be `@require_operator` under tiered policy
- `settings_views.py:135` `toggle_alert_rule` — same as above

All 19 other audited write endpoints already match policy.

## 3. Goals & Non-Goals

### 3.1 Goals

- **G1.** Close the tenant-isolation leak in `TenantMiddleware` with a fail-closed design that distinguishes API from web clients.
- **G2.** Make `cost_engine.compute_cost_snapshot()` per-org atomic.
- **G3.** Finish the `settings_views.py` `reverse()` migration.
- **G4.** Add the `AlertRuleForm` Slack webhook validator.
- **G5.** Introduce the `@require_operator` decorator and loosen alert-rule create/toggle to operator+.
- **G6.** Add the `@require_admin` decorator on `settings_api_keys` as defense-in-depth.
- **G7.** Publish the permission matrix at `docs/security/permission-matrix.md` as the source of truth for future audits.
- **G8.** Add regression tests for every fix, following the house style (Django TestCase + hand-rolled helpers).
- **G9.** Produce a smoke test that proves the real Go agent still authenticates against the updated auth + middleware stack.

### 3.2 Non-Goals (deferred to other specs)

- Splitting `services/llm_sync_engine.py` (436 LOC) — **A1**
- Splitting `views/settings_views.py` (524 LOC) — **A1**
- Splitting `models/organization.py` (257 LOC) — **A1**
- Refactoring denormalized GPU snapshot fields — **A1 or B'**
- TimescaleDB continuous aggregates and materialized views — **B'**
- `select_related` / `prefetch_related` optimization sweep — **B'**
- Rate limiting on ingest endpoints — **E'**
- LLM API key rotation — **E'**
- Audit log for admin actions — **E'**
- SSO / SAML — **E'**
- Full tenant-scoping audit of Celery tasks and admin site — **E0** (deferred future spec)
- Converting tests to pytest-django — **out of program** (house style is Django TestCase)
- DRF-ifying the public REST API — **D** (future feature spec)
- Refactoring the `save_user_profile` signal — **not needed** (research confirmed no-op is correct)

## 4. Design

### 4.1 TenantMiddleware — dual-mode fail-closed (G1)

Replaces the silent-swallow behavior with an allowlist-based fail-closed design that distinguishes API from web clients.

**Behavior matrix:**

| Path | Authenticated? | Org resolved? | Middleware action |
|------|----------------|---------------|-------------------|
| In allowlist (exact or prefix) | any | any | Skip tenant resolution entirely; run view |
| Not in allowlist | No | n/a | Pass through (Django auth middleware handles) |
| Not in allowlist | Yes | Yes | `set_current_org(org)`; run view; clear in `finally` |
| Not in allowlist, starts with `/api/` | Yes | No | Log warning; return `JsonResponse({"error": "no_organization", ...}, status=403)` |
| Not in allowlist, web path | Yes | No | Log warning; redirect to `monitor:no_organization` |

**Allowlist:**

```python
ALLOWLIST_EXACT = frozenset({
    "/",
    "/accounts/login/",
    "/accounts/logout/",
    "/accounts/signup/",
    "/accounts/password_reset/",
    "/api/health/",
    "/api/ready/",
    "/onboarding/",
    "/no-organization/",
})
ALLOWLIST_PREFIX = (
    "/accounts/accept-invite/",
    "/static/",
    "/media/",
)
```

**Invariant:** a view function is never reached with an unset tenant context for any authenticated, non-allowlisted request path. This is the security guarantee tested in §7.

**New companion artifacts:**

- `monitor/views/onboarding_views.py` — one view function `no_organization(request)` rendering a minimal template
- `monitor/templates/no_organization.html` — user-facing "your account has no organization assigned" page with a support contact link
- `monitor:no_organization` URL name at `/no-organization/` added to `monitor/urls.py`

### 4.2 cost_engine — per-org atomic snapshot (G2)

Wrap the per-org loop body in `cost_engine.py:57-101` in `transaction.atomic(using='default')` so each org's snapshot is atomic. The boundary is **per-org**, not across all orgs — this preserves TimescaleDB write throughput and means a failure in one org does not roll back snapshots already written for other orgs.

```python
for org in orgs:
    with transaction.atomic(using='default'):
        gpus = GPU.objects_unscoped.filter(...).iterator(chunk_size=500)
        rows = [...]
        if rows:
            with connection.cursor() as cur:
                execute_values(cur, SQL, rows)
```

On rollback: log at `ERROR` level with the org slug in the message.

### 4.3 cost_engine — parameterize INTERVAL_SECONDS (red flag #1)

Replace the f-string interpolation at `cost_engine.py:161`:

```python
# Before
COUNT(*) * {INTERVAL_SECONDS} / 3600.0 AS gpu_hours

# After
COUNT(*) * %s / 3600.0 AS gpu_hours
```

Add `INTERVAL_SECONDS` to the params tuple at the call site. Not exploitable, but keeps all cost_engine SQL consistent.

### 4.4 settings_views.py — finish reverse() migration (G3)

Grep for any remaining hardcoded `"/settings/..."` literals in `redirect()` calls within `settings_views.py`. Replace each with `reverse("monitor:<name>")`. The migration is partially done in the in-flight batch; A0 finishes it.

### 4.5 AlertRuleForm — Slack webhook validator (G4)

Copy the existing `validate_slack_webhook_url` validator from `monitor/models/alert.py` into `monitor/forms.py` and apply it as a field-level validator on the `slack_webhook_url` field of `AlertRuleForm`. Surfaces form errors with a user-friendly message:

> `"Must be a valid https://hooks.slack.com/ webhook URL"`

### 4.6 @require_operator decorator (G5)

New decorator in `monitor/decorators.py`:

```python
def require_operator(view_func):
    """Allow owner, admin, or operator roles. Blocks viewer."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("accounts:login")
        profile = getattr(request.user, "profile", None)
        if profile is None or profile.role not in {"owner", "admin", "operator"}:
            return HttpResponseForbidden("Operator role or higher required")
        return view_func(request, *args, **kwargs)
    return _wrapped
```

Mirrors the shape of the existing `@require_admin` decorator. Applied to:

- `settings_views.py:110` `create_alert_rule`
- `settings_views.py:135` `toggle_alert_rule`

`delete_alert_rule` (line 149) keeps `@require_admin` — delete is admin-only.

### 4.7 settings_api_keys — @require_admin defense-in-depth (G6)

Add `@require_admin` decorator to `settings_views.py:55` `settings_api_keys` POST handler. Leave the existing inline `_is_admin()` check in place as defense-in-depth.

### 4.8 Permission matrix document (G7)

New file `docs/security/permission-matrix.md` documenting every write endpoint with its required role under the tiered policy. Table columns: `Endpoint | URL name | HTTP method | Required role | Decorator | Notes`. Generated once, maintained manually alongside future endpoint additions.

Acts as the acceptance target for any future permission audit.

## 5. RBAC Policy — Tiered

| Role | Can do |
|------|--------|
| **Owner** | Everything Admin+ can do, **plus**: delete org, transfer ownership, assign `owner` role to another user |
| **Admin+** (owner, admin) | Invite users (non-owner roles), create/revoke API keys, add/remove LLM providers, add/remove GPU clusters/nodes/endpoints, delete alert rules, change member roles (non-owner), manage all settings |
| **Operator+** (owner, admin, operator) | Create/edit/toggle alert rules (not delete), acknowledge and resolve alerts, edit inference endpoint configuration |
| **Viewer+** (any authenticated user) | Read everything within their org |

**Endpoints changed by A0:**

| Endpoint | Before A0 | After A0 |
|----------|-----------|----------|
| `settings_api_keys` POST | inline `_is_admin()` only | `@require_admin` + inline |
| `create_alert_rule` | `@require_admin` | `@require_operator` |
| `toggle_alert_rule` | `@require_admin` | `@require_operator` |

All 19 other audited write endpoints match the tiered policy without modification.

## 6. Data Flow

**Before A0 (broken tenant-isolation):**

```
Request → AuthMiddleware → TenantMiddleware (swallows exceptions) → View with UNSET tenant → Query returns cross-org rows
```

**After A0:**

```
Request → AuthMiddleware → TenantMiddleware:
  1. Path in allowlist?        → skip tenant resolution, run view
  2. Unauthenticated?          → run view (Django auth handles access)
  3. Auth + org resolved?      → set_current_org, run view, clear in finally
  4. Auth + org unresolved?
     a. Path starts with /api/ → JsonResponse(..., status=403), NEVER run view
     b. Web path               → redirect to /no-organization/, NEVER run view
```

Security invariant: view functions are **never** reached with an unset tenant context for authenticated non-allowlisted paths.

## 7. Testing Strategy

### 7.1 Framework and style

- **Framework:** Django TestCase (matches existing `monitor/tests/` house style; no pytest-django migration)
- **DB:** SQLite in-memory (matches existing tests; TimescaleDB-specific features are skipped via the existing fallback pattern)
- **Factories:** Inline `_make_user`, `_make_org`, `_make_alert_rule` helpers following `test_settings_views.py` conventions
- **Auth:** `self.client.force_login(user)`
- **Tenant scoping:** Tests exercise the real middleware stack end-to-end — they do not bypass it with direct `set_current_org()` calls
- **Mocking:** `@patch` on `requests.post` (Slack), assertions on `logging` warnings via `assertLogs`

### 7.2 Test suite — `monitor/tests/test_a0_stabilization.py`

| # | Test | Asserts |
|---|------|---------|
| T1 | `test_tenant_middleware_fails_closed_on_missing_profile` | Authenticated user with no `UserProfile` row → 403 JSON for `/api/*`, redirect for web |
| T2 | `test_tenant_middleware_fails_closed_on_null_organization` | Authenticated user with profile but `organization=None` → same fail-closed behavior |
| T3 | `test_tenant_middleware_json_response_for_api_paths` | `/api/some-endpoint/` returns `{"error": "no_organization"}`, status 403, `Content-Type: application/json` |
| T4 | `test_tenant_middleware_redirects_for_web_paths` | `/dashboard/` redirects to `/no-organization/` with 302 |
| T5 | `test_tenant_middleware_allowlist_health_endpoints_work_unauth` | `/api/health/` and `/api/ready/` return 200 for unauthenticated requests |
| T6 | `test_tenant_middleware_allowlist_accept_invite_with_prefix` | `/accounts/accept-invite/<token>/` reachable unauthenticated |
| T7 | `test_tenant_middleware_cross_org_query_blocked` | Red-team: user in org A cannot read a GPU belonging to org B even if they know its UUID |
| T8 | `test_cost_engine_partial_write_rollback` | Mock `execute_values` to raise on second org; first org's snapshot committed, second rolled back |
| T9 | `test_alert_rule_form_rejects_non_slack_url` | `AlertRuleForm({"slack_webhook_url": "https://evil.com/hook"})` → `is_valid()` False |
| T10 | `test_alert_rule_form_accepts_valid_slack_url` | `https://hooks.slack.com/services/T.../B.../...` valid |
| T11 | `test_settings_api_keys_requires_admin_decorator` | Operator role → 403 on POST; admin role → 200 |
| T12 | `test_operator_can_create_alert_rule` | Operator role → 200 on `create_alert_rule` POST |
| T13 | `test_operator_can_toggle_alert_rule` | Operator role → 200 on `toggle_alert_rule` POST |
| T14 | `test_operator_cannot_delete_alert_rule` | Operator role → 403 on `delete_alert_rule` POST |
| T15 | `test_viewer_cannot_create_alert_rule` | Viewer role → 403 on `create_alert_rule` POST |
| T16 | `test_go_agent_smoke_authenticates_real_header` | Subprocess smoke test; `pytest.skip` if binary absent |

**Estimated LOC:** ~400 for the test file.

### 7.3 Pre-deploy check

Before deploying A0 to production, run:

```sql
SELECT COUNT(*) FROM auth_user u
LEFT JOIN monitor_userprofile p ON p.user_id = u.id
WHERE p.id IS NULL OR p.organization_id IS NULL;
```

Expected: 0. If non-zero, users exist in the database who would hit the new fail-closed path on their next request. Decide per user whether to delete, assign to an org, or accept the onboarding flow.

## 8. Error Handling

- **`TenantMiddleware`** logs at `WARNING` (not `ERROR`) for tenant resolution failures. Log line: `"Tenant resolution failed for authenticated user %s on %s"`.
- **`cost_engine.compute_cost_snapshot`** logs at `ERROR` with org slug on `transaction.atomic` rollback.
- **`AlertRuleForm.clean_slack_webhook_url`** raises `ValidationError("Must be a valid https://hooks.slack.com/ webhook URL")`.
- **Go agent smoke test** uses `pytest.skip` if the agent binary is absent.

## 9. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Production user has no org → sees `/no-organization/` unexpectedly | Low | Low | Pre-deploy SQL check in §7.3 |
| Allowlist misses a public URL → user sees 403 | Medium | Medium | Integration tests T5, T6 hit every public URL |
| Loosening alert rule creation to operator breaks an existing test | Low | Low | Run full suite before commit |
| Go agent smoke test fails on CI without Go toolchain | Medium | Low | Skip-if-binary-absent |
| Per-org atomic in cost_engine measurably slower | Low | Low | Per-org boundary (not cross-org) minimizes lock scope |
| `AttributeError` catch on profile masks a different bug | Low | Low | Paired with `logger.warning` |
| Deploy leaves old agents with ops unaware | Low | Low | PR description includes `X-API-Key` operational note |

## 10. Deploy Plan

Not part of the A0 code, but noted so the implementation plan accounts for it:

1. Run pre-deploy SQL check (§7.3) on production database
2. Merge A0 PR to `master`
3. Run migration `0012_perf_indexes_and_retention` on production (already in the in-flight batch)
4. Deploy via `server-deploy.sh`
5. Verify `/api/health/` → 200 and `/api/ready/` → 200 through the nginx → ALB → gunicorn path
6. Tail Django logs for "Tenant resolution failed" warnings — should be zero in steady state
7. Rollback plan: `git revert <merge-sha>` and redeploy

A0 adds **no new migrations**.

## 11. Footprint Estimate

| Area | New LOC | Modified LOC |
|------|---------|--------------|
| `monitor/middleware.py` | +55 | -5 |
| `monitor/views/onboarding_views.py` | +20 | 0 |
| `monitor/templates/no_organization.html` | +30 | 0 |
| `monitor/urls.py` | +1 | 0 |
| `monitor/services/cost_engine.py` | +8 | -2 |
| `monitor/views/settings_views.py` | 0 | ~30 |
| `monitor/decorators.py` | +15 | 0 |
| `monitor/forms.py` | +8 | 0 |
| `monitor/tests/test_a0_stabilization.py` | +400 | 0 |
| `docs/security/permission-matrix.md` | +100 | 0 |
| **Total** | **~637** | **~37** |

One PR. Conventional commits grouped by theme:

1. `fix: TenantMiddleware fails closed with dual-mode API/web response`
2. `fix: wrap cost_engine per-org snapshot in transaction.atomic`
3. `fix: parameterize remaining f-string SQL in cost_engine`
4. `refactor: finish settings_views reverse() migration`
5. `feat: require_operator decorator + tiered alert rule RBAC`
6. `fix: add Slack webhook validator to AlertRuleForm`
7. `test: A0 stabilization regression suite`
8. `docs: permission matrix`

## 12. Open Questions

None remaining after brainstorming. All design decisions are locked.

## 13. Next Step

After this spec is committed and the user has reviewed the written file, invoke the `superpowers:writing-plans` skill to produce a step-by-step implementation plan.
