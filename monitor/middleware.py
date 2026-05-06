"""
monitor/middleware.py -- Tenant-scoping middleware with fail-closed enforcement.

Sets the current organization from the authenticated user's profile so that
TenantManager auto-filters all ORM queries by org. Fails closed if tenant
resolution fails — distinguishing API (JSON 403) from web (redirect) clients.

Security invariant:
    A view function is never reached with an unset tenant context for any
    authenticated, non-allowlisted request path.

Allowlist:
    Paths in ALLOWLIST_EXACT or starting with ALLOWLIST_PREFIX skip tenant
    resolution entirely. Used for landing page, auth flows, health checks,
    static files, the no-organization page itself, and the accept-invite
    token flow.
"""
import logging

from django.http import JsonResponse
from django.shortcuts import redirect

from monitor.models.base import clear_current_org, set_current_org

logger = logging.getLogger(__name__)


ALLOWLIST_EXACT = frozenset({
    "/",
    "/api/health/",
    "/api/ready/",
    "/no-organization/",
    "/onboarding/enable-demo/",
})

# `/accounts/` covers Django contrib.auth URLs (login, logout, signup,
# password_reset, password_reset/done, reset/<uidb64>/<token>/, reset/done/)
# AND monitor/urls_accounts.py (accept-invite). Admin is intentionally NOT
# allowlisted — superusers still need tenant context via their profile.
ALLOWLIST_PREFIX = (
    "/accounts/",
    "/static/",
    "/media/",
)


def _is_allowlisted(path: str) -> bool:
    if path in ALLOWLIST_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in ALLOWLIST_PREFIX)


class TenantMiddleware:
    """
    Must be placed AFTER AuthenticationMiddleware in settings.MIDDLEWARE.

    Behavior:
      - Allowlisted paths: skip tenant resolution entirely.
      - Unauthenticated non-allowlisted: pass through (Django auth handles).
      - Authenticated with resolved org: set_current_org, run view, clear.
      - Authenticated with unresolved org:
          - /api/*     → JsonResponse({"error": "no_organization"}, status=403)
          - Web path   → redirect to monitor:no_organization
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # 1. Allowlisted paths skip tenant resolution entirely.
        if _is_allowlisted(path):
            return self.get_response(request)

        # 2. Unauthenticated: let Django auth middleware handle access.
        if not request.user.is_authenticated:
            return self.get_response(request)

        # 3. Authenticated: resolve org or fail closed.
        org = None
        try:
            profile = request.user.profile
            org = profile.organization
        except Exception:  # noqa: BLE001 — intentional broad catch (missing profile, etc.)
            org = None

        if org is None:
            logger.warning(
                "Tenant resolution failed for authenticated user %s on %s",
                request.user.pk, path,
            )
            if path.startswith("/api/"):
                return JsonResponse(
                    {"error": "no_organization",
                     "detail": "User has no assigned organization"},
                    status=403,
                )
            return redirect("monitor:no_organization")

        # 4. Happy path: set tenant, run view, always clear.
        set_current_org(org)
        try:
            return self.get_response(request)
        finally:
            clear_current_org()
