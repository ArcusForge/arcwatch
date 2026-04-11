"""
monitor/tests/test_a0_stabilization.py -- Regression suite for A0 (stabilize
in-flight work). See docs/superpowers/specs/2026-04-10-a0-stabilize-in-flight-work-design.md
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from monitor.models import Organization

User = get_user_model()


# ── Shared test helpers ───────────────────────────────────────────────────────

def _make_org(slug="test-org"):
    """Create an Organization with an auto-created owner user for tests that need one."""
    owner = User.objects.create_user(username=f"owner-of-{slug}", password="pw")
    return Organization.objects.create(name=f"Test {slug}", slug=slug, owner=owner)


def _make_user_with_role(username, role, org):
    """Create a User whose signal-created UserProfile is attached to *org* with *role*."""
    user = User.objects.create_user(username=username, password="pw")
    # The post_save signal creates a UserProfile automatically; assign org + role.
    user.profile.organization = org
    user.profile.role = role
    user.profile.save()
    return user


# ── Task 2: require_operator decorator ────────────────────────────────────────

class RequireOperatorDecoratorTest(TestCase):
    def setUp(self):
        from monitor.decorators import require_operator

        self.factory = RequestFactory()
        self.org = _make_org("ro-decorator")

        @require_operator
        def protected_view(request):
            return HttpResponse("ok")

        self.protected_view = protected_view

    def _request_as(self, user):
        request = self.factory.post("/x/")
        request.user = user
        return request

    def test_require_operator_allows_owner(self):
        user = _make_user_with_role("owner-u", "owner", self.org)
        self.assertEqual(self.protected_view(self._request_as(user)).status_code, 200)

    def test_require_operator_allows_admin(self):
        user = _make_user_with_role("admin-u", "admin", self.org)
        self.assertEqual(self.protected_view(self._request_as(user)).status_code, 200)

    def test_require_operator_allows_operator(self):
        user = _make_user_with_role("op-u", "operator", self.org)
        self.assertEqual(self.protected_view(self._request_as(user)).status_code, 200)

    def test_require_operator_blocks_viewer(self):
        user = _make_user_with_role("view-u", "viewer", self.org)
        self.assertEqual(self.protected_view(self._request_as(user)).status_code, 403)

    def test_require_operator_blocks_unauthenticated(self):
        request = self.factory.post("/x/")
        request.user = AnonymousUser()
        self.assertEqual(self.protected_view(request).status_code, 403)


# ── Task 3: onboarding view ───────────────────────────────────────────────────

class NoOrganizationViewTest(TestCase):
    def test_no_organization_view_renders(self):
        user = User.objects.create_user(username="orphan-user", password="pw")
        # user.profile exists via signal but has no organization
        self.client.force_login(user)
        response = self.client.get("/no-organization/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"no organization", response.content.lower())

    def test_no_organization_view_url_name_reversible(self):
        from django.urls import reverse
        self.assertEqual(reverse("monitor:no_organization"), "/no-organization/")


# ── Task 4: TenantMiddleware fail-closed ──────────────────────────────────────

class TenantMiddlewareTest(TestCase):
    """
    Integration tests that exercise the real middleware stack.
    Do NOT bypass the middleware with direct set_current_org() calls —
    these tests exist specifically to verify the middleware itself.
    """

    def setUp(self):
        self.org = _make_org("mw-test")

    def test_tenant_middleware_fails_closed_on_missing_profile(self):
        """User with no profile hitting a web path → redirect to /no-organization/."""
        user = User.objects.create_user(username="no-profile", password="pw")
        # Delete the auto-created profile to simulate the missing-profile path
        user.profile.delete()
        self.client.force_login(user)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/no-organization/", response["Location"])

    def test_tenant_middleware_fails_closed_on_null_organization(self):
        """User with profile but org=None → redirect to /no-organization/."""
        user = User.objects.create_user(username="no-org", password="pw")
        # signal-created profile exists with organization=None by default
        self.client.force_login(user)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/no-organization/", response["Location"])

    def test_tenant_middleware_json_response_for_api_paths(self):
        """API path with no org → 403 JSON, not an HTML redirect."""
        import json
        user = User.objects.create_user(username="api-no-org", password="pw")
        self.client.force_login(user)
        response = self.client.get("/api/v1/ingest/gpu/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Content-Type"], "application/json")
        body = json.loads(response.content)
        self.assertEqual(body["error"], "no_organization")

    def test_tenant_middleware_redirects_for_web_paths(self):
        """Web path with no org → 302 redirect to /no-organization/."""
        user = User.objects.create_user(username="web-no-org", password="pw")
        self.client.force_login(user)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/no-organization/", response["Location"])

    def test_tenant_middleware_allowlist_health_endpoints_work_unauth(self):
        """Health endpoints must be reachable without auth and without a tenant."""
        response_health = self.client.get("/api/health/")
        self.assertEqual(response_health.status_code, 200)
        response_ready = self.client.get("/api/ready/")
        # ready may be 200 or 503 depending on DB/redis; both are non-auth responses
        self.assertIn(response_ready.status_code, (200, 503))

    def test_tenant_middleware_allowlist_landing_page_works_unauth(self):
        """The public landing page `/` must be reachable unauthenticated."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_tenant_middleware_sets_org_for_resolved_user(self):
        """Authenticated user with a valid org → view runs successfully."""
        user = _make_user_with_role("resolved-user", "admin", self.org)
        self.client.force_login(user)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)


class TenantMiddlewareCrossOrgLeakTest(TestCase):
    """Red-team test: verify tenant isolation cannot leak across orgs."""

    def test_cross_org_query_blocked(self):
        """User in org A cannot see GPUs belonging to org B via the dashboard."""
        from monitor.models import GPU, GPUCluster, GPUNode

        org_a = _make_org("org-a")
        org_b = _make_org("org-b")

        # Create a GPU in org B
        cluster_b = GPUCluster.objects_unscoped.create(organization=org_b, name="b-cluster")
        node_b = GPUNode.objects_unscoped.create(
            organization=org_b, cluster=cluster_b,
            hostname="b-node-1", gpu_count=1, gpu_type="H100",
        )
        gpu_b = GPU.objects_unscoped.create(
            organization=org_b, node=node_b,
            uuid="GPU-B-UUID-0001", gpu_index=0,
            current_model_name="H100 SXM", status="healthy",
        )

        # User in org A requests the dashboard — must not see GPU_B
        user_a = _make_user_with_role("user-in-a", "viewer", org_a)
        self.client.force_login(user_a)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"GPU-B-UUID-0001", response.content)
        self.assertNotIn(b"b-node-1", response.content)
