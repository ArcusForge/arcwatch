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
