# ArcWatch Permission Matrix

> **Source of truth for RBAC.** Every write endpoint is listed with its required role.
> Update this table whenever you add, remove, or re-gate a write endpoint. Future
> permission audits use this as the acceptance target.

## Role hierarchy

```
viewer < operator < admin < owner
```

| Role | Typical user |
|------|--------------|
| **viewer** | Read-only observer (PM, leadership, auditor) |
| **operator** | Day-to-day fleet manager — creates alert rules, responds to incidents |
| **admin** | Org administrator — manages resources, API keys, members, LLM providers |
| **owner** | Org owner — structural changes, ownership transfer, destructive actions |

## Role capabilities

- **Owner:** everything admin+ can do, **plus** delete org, transfer ownership, assign `owner` role to another user
- **Admin+:** invite users (non-owner roles), create/revoke API keys, add/remove LLM providers, add/remove GPU clusters/nodes/endpoints, delete alert rules, change member roles (non-owner), manage all settings
- **Operator+:** create/edit/toggle alert rules (not delete), acknowledge and resolve alerts, edit inference endpoint configuration
- **Viewer+:** read everything within their org

## Write endpoints

| # | URL name | HTTP | View function | File | Required role | Decorator / check |
|---|----------|------|---------------|------|---------------|-------------------|
| 1 | `monitor:settings_api_keys` | POST | `settings_api_keys` | `monitor/views/settings_views.py` | admin+ | `@login_required` + inline `_is_admin()` + defense-in-depth role tuple check |
| 2 | `monitor:revoke_api_key` | POST | `revoke_api_key` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 3 | `monitor:create_alert_rule` | POST | `create_alert_rule` | `monitor/views/settings_views.py` | **operator+** | `@login_required` + `@require_operator` |
| 4 | `monitor:toggle_alert_rule` | POST | `toggle_alert_rule` | `monitor/views/settings_views.py` | **operator+** | `@login_required` + `@require_operator` |
| 5 | `monitor:delete_alert_rule` | POST | `delete_alert_rule` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 6 | `monitor:create_cluster` | POST | `create_cluster` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 7 | `monitor:deactivate_cluster` | POST | `deactivate_cluster` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 8 | `monitor:delete_cluster` | POST | `delete_cluster` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 9 | `monitor:deactivate_node` | POST | `deactivate_node` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 10 | `monitor:delete_node` | POST | `delete_node` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 11 | `monitor:create_endpoint` | POST | `create_endpoint` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 12 | `monitor:deactivate_endpoint` | POST | `deactivate_endpoint` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 13 | `monitor:delete_endpoint` | POST | `delete_endpoint` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 14 | `monitor:change_member_role` | POST | `change_member_role` | `monitor/views/settings_views.py` | admin+ (inline owner-only check for assigning owner role) | `@login_required` + `@require_admin` + inline owner-role check |
| 15 | `monitor:remove_member` | POST | `remove_member` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 16 | `monitor:invite_member` | POST | `invite_member` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 17 | `monitor:revoke_invite` | POST | `revoke_invite` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 18 | `monitor:resend_invite` | POST | `resend_invite` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 19 | `monitor:create_llm_provider` | POST | `create_llm_provider` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 20 | `monitor:delete_llm_provider` | POST | `delete_llm_provider` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 21 | `monitor:toggle_llm_provider` | POST | `toggle_llm_provider` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 22 | `monitor:sync_llm_provider` | POST | `sync_llm_provider` | `monitor/views/settings_views.py` | admin+ | `@login_required` + `@require_admin` |
| 23 | `accept_invite` (no URL name — token-gated public flow) | POST | `accept_invite` | `monitor/views/settings_views.py` | public (token gated) | no role decorator — validated by invite token expiry + username collision check |

## Ingest endpoints (API key gated, not role gated)

| URL name | HTTP | File | Auth | Notes |
|----------|------|------|------|-------|
| `monitor:api_ingest_gpu` | POST | `monitor/rest_api.py` | `X-Api-Key` header | Validated by `monitor/api_auth.py` |
| `monitor:api_ingest_inference` | POST | `monitor/rest_api.py` | `X-Api-Key` header | Validated by `monitor/api_auth.py` |

## Health endpoints (unauthenticated, allowlisted in TenantMiddleware)

| URL name | HTTP | File | Auth | Notes |
|----------|------|------|------|-------|
| `monitor:healthz` | GET | `monitor/views/health_views.py` | none | In `TenantMiddleware.ALLOWLIST_EXACT`; always 200 |
| `monitor:readyz` | GET | `monitor/views/health_views.py` | none | Allowlisted; returns 200 or 503 based on DB + Redis connectivity |

## TenantMiddleware allowlist (A0 §4.1)

Paths that skip tenant resolution entirely:

**Exact match (`ALLOWLIST_EXACT`):**

- `/` — public landing page
- `/api/health/` — liveness
- `/api/ready/` — readiness
- `/no-organization/` — fail-closed redirect target for authenticated users with no org

**Prefix match (`ALLOWLIST_PREFIX`):**

- `/accounts/` — Django auth URLs (login, logout, signup, password_reset, password_reset/done, reset/<token>/, reset/done/) AND `monitor/urls_accounts.py:accept_invite`
- `/static/` — WhiteNoise static files
- `/media/` — user uploads (if any)

**Intentionally NOT allowlisted:**

- `/admin/` — Django admin. Superusers still need tenant context via their profile.organization, and a superuser without a profile.organization will correctly hit the fail-closed redirect.

## Update process

When adding a new write endpoint:

1. Add a row to the **Write endpoints** table above with the required role under the tiered policy.
2. Apply the matching decorator:
   - `@require_admin` — admin and owner roles only
   - `@require_operator` — operator, admin, and owner roles
   - No decorator — read-only access (any authenticated user in the org)
3. Add a regression test to `monitor/tests/test_a0_stabilization.py` (or a successor test file) that asserts each role gets the expected status code.
4. Update this file and commit in the same PR.

When adding a new unauthenticated public path:

1. Add the path to `ALLOWLIST_EXACT` (or `ALLOWLIST_PREFIX` if it's parameterized) in `monitor/middleware.py`.
2. Add the path to the **TenantMiddleware allowlist** section above.
3. Add a test to `TenantMiddlewareTest` in `monitor/tests/test_a0_stabilization.py` that verifies the path is reachable unauthenticated.
