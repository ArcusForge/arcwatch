"""
monitor/tests/test_demo_seeder.py

Tests for monitor.services.demo_seeder.seed_demo_fleet and the
enable_demo_fleet view.
"""
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase

from monitor.models import (
    AlertRule,
    AlertEvent,
    GPU,
    GPUCluster,
    GPUNode,
    InferenceEndpoint,
    Organization,
)
from monitor.services.demo_seeder import seed_demo_fleet


def _make_user_and_org(slug):
    user = User.objects.create_user(username=slug, password="pw")
    org = Organization.objects.create(name=slug, slug=slug, owner=user)
    profile = user.profile
    profile.organization = org
    profile.role = "owner"
    profile.save()
    return user, org


def _count_gpu_metrics():
    with connection.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM gpu_metrics")
        return cur.fetchone()[0]


def _count_inference_metrics():
    with connection.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM inference_metrics")
        return cur.fetchone()[0]


def _count_cost_snapshots():
    with connection.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM cost_snapshots")
        return cur.fetchone()[0]


class SeedDemoFleetServiceTest(TestCase):
    def setUp(self):
        self.user, self.org = _make_user_and_org("seed_org")

    def test_seed_creates_expected_resources(self):
        result = seed_demo_fleet(
            self.org, user=self.user, nodes=2, gpus_per_node=2, hours=1,
        )
        self.assertEqual(result["nodes"], 2)
        self.assertEqual(result["gpus"], 4)
        self.assertEqual(result["endpoints"], 4)
        self.assertEqual(result["alert_rules"], 3)
        self.assertEqual(result["hours"], 1)

        # Cluster created with default name
        self.assertTrue(
            GPUCluster.objects_unscoped.filter(
                organization=self.org, name="Demo Fleet"
            ).exists()
        )
        # Nodes + GPUs scoped to org
        self.assertEqual(
            GPUNode.objects_unscoped.filter(organization=self.org).count(), 2
        )
        self.assertEqual(
            GPU.objects_unscoped.filter(organization=self.org).count(), 4
        )
        # Endpoints + rules + events
        self.assertEqual(
            InferenceEndpoint.objects_unscoped.filter(organization=self.org).count(), 4
        )
        self.assertEqual(AlertRule.objects.filter(organization=self.org).count(), 3)
        self.assertEqual(
            AlertEvent.objects.filter(rule__organization=self.org).count(), 3
        )

    def test_hypertable_backfill_row_counts(self):
        result = seed_demo_fleet(
            self.org, user=self.user, nodes=2, gpus_per_node=2, hours=2,
        )
        # 2 nodes × 2 GPUs × 2 hours × 60 min = 480 GPU rows
        self.assertEqual(result["gpu_metrics_rows"], 480)
        # 4 endpoints × 2 hours × 60 min = 480 inference rows
        self.assertEqual(result["inference_metrics_rows"], 480)
        # 4 GPUs × 2 hours × 60 min = 480 cost snapshot rows
        self.assertEqual(result["cost_snapshot_rows"], 480)

    def test_idempotent_resources(self):
        # First seed
        r1 = seed_demo_fleet(
            self.org, user=self.user, nodes=2, gpus_per_node=2, hours=1,
        )
        # Re-seed with same parameters
        r2 = seed_demo_fleet(
            self.org, user=self.user, nodes=2, gpus_per_node=2, hours=1,
        )
        # Resource counts should be identical (get_or_create semantics)
        self.assertEqual(
            GPUCluster.objects_unscoped.filter(organization=self.org).count(), 1
        )
        self.assertEqual(
            GPUNode.objects_unscoped.filter(organization=self.org).count(), 2
        )
        self.assertEqual(
            GPU.objects_unscoped.filter(organization=self.org).count(), 4
        )
        self.assertEqual(r1["gpus"], r2["gpus"])

    def test_org_isolation(self):
        _, org_a = _make_user_and_org("iso_a")
        _, org_b = _make_user_and_org("iso_b")
        seed_demo_fleet(org_a, nodes=2, gpus_per_node=2, hours=1)
        seed_demo_fleet(org_b, nodes=2, gpus_per_node=2, hours=1)
        # Each org has its own cluster, nodes, GPUs
        self.assertEqual(GPU.objects_unscoped.filter(organization=org_a).count(), 4)
        self.assertEqual(GPU.objects_unscoped.filter(organization=org_b).count(), 4)
        # No GPU UUID collision (uuids include org slug)
        a_uuids = set(GPU.objects_unscoped.filter(organization=org_a).values_list("uuid", flat=True))
        b_uuids = set(GPU.objects_unscoped.filter(organization=org_b).values_list("uuid", flat=True))
        self.assertFalse(a_uuids & b_uuids)

    def test_invalid_args_raise(self):
        with self.assertRaises(ValueError):
            seed_demo_fleet(self.org, nodes=0)
        with self.assertRaises(ValueError):
            seed_demo_fleet(self.org, hours=9999)
        with self.assertRaises(ValueError):
            seed_demo_fleet(self.org, gpus_per_node=99)


class EnableDemoFleetViewTest(TestCase):
    def test_login_required(self):
        client = Client()
        r = client.post("/onboarding/enable-demo/")
        # Redirects to login (302 to /accounts/login/)
        self.assertIn(r.status_code, (302, 401))

    def test_get_method_not_allowed(self):
        user = User.objects.create_user(username="m_user", password="pw")
        client = Client()
        client.login(username="m_user", password="pw")
        r = client.get("/onboarding/enable-demo/")
        self.assertEqual(r.status_code, 405)

    def test_creates_personal_org_for_user_without_one(self):
        user = User.objects.create_user(username="newbie", password="pw")
        # newbie has no org
        self.assertIsNone(user.profile.organization)
        client = Client()
        client.login(username="newbie", password="pw")

        r = client.post("/onboarding/enable-demo/")
        self.assertEqual(r.status_code, 302)

        # Org now exists, profile updated
        user.refresh_from_db()
        user.profile.refresh_from_db()
        self.assertIsNotNone(user.profile.organization)
        self.assertEqual(user.profile.role, "owner")

        org = user.profile.organization
        # Demo Fleet cluster + GPUs seeded
        self.assertTrue(
            GPUCluster.objects_unscoped.filter(
                organization=org, name="Demo Fleet"
            ).exists()
        )
        self.assertEqual(
            GPU.objects_unscoped.filter(organization=org).count(), 16,
        )

    def test_uses_existing_org(self):
        user, org = _make_user_and_org("has_org")
        client = Client()
        client.login(username="has_org", password="pw")
        r = client.post("/onboarding/enable-demo/")
        self.assertEqual(r.status_code, 302)
        # Same org, no new org created
        self.assertEqual(Organization.objects.filter(slug="has_org").count(), 1)
        # Demo Fleet cluster created in their existing org
        self.assertTrue(
            GPUCluster.objects_unscoped.filter(organization=org, name="Demo Fleet").exists()
        )

    def test_idempotent_view(self):
        user, org = _make_user_and_org("repeat_user")
        client = Client()
        client.login(username="repeat_user", password="pw")
        client.post("/onboarding/enable-demo/")
        client.post("/onboarding/enable-demo/")
        # Still exactly one Demo Fleet cluster, 16 GPUs
        self.assertEqual(
            GPUCluster.objects_unscoped.filter(organization=org, name="Demo Fleet").count(), 1
        )
        self.assertEqual(GPU.objects_unscoped.filter(organization=org).count(), 16)
