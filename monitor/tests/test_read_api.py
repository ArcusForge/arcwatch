"""
monitor/tests/test_read_api.py

Tests for the public Read API v1: GET /api/v1/clusters|nodes|gpus|inference/endpoints|costs/summary|alerts/*.

Coverage per endpoint: auth (401), wrong scope (403), tenant isolation, pagination.
"""
import base64

from django.contrib.auth.models import User
from django.test import Client, TestCase

from monitor.models import (
    APIKey,
    AlertRule,
    AlertEvent,
    GPU,
    GPUCluster,
    GPUNode,
    InferenceEndpoint,
    Organization,
)


def _make_user_and_org(slug, role="owner"):
    user = User.objects.create_user(username=slug, password="pw")
    org = Organization.objects.create(name=slug, slug=slug, owner=user)
    profile = user.profile
    profile.organization = org
    profile.role = role
    profile.save()
    return user, org


def _make_read_key(org, user):
    api_key, raw = APIKey.create_key(org, user, name="read-key", scopes=["read"])
    return api_key, raw


def _make_ingest_key(org, user):
    api_key, raw = APIKey.create_key(org, user, name="ingest-key", scopes=["ingest"])
    return api_key, raw


def _seed_minimal(org, n_clusters=1, n_nodes=2, n_gpus=3):
    """Seed a small fleet for list tests."""
    clusters, nodes, gpus = [], [], []
    for c in range(n_clusters):
        cluster = GPUCluster.objects_unscoped.create(
            organization=org, name=f"c{c}", cloud="aws", region="us-east-1",
        )
        clusters.append(cluster)
        for n in range(n_nodes):
            node = GPUNode.objects_unscoped.create(
                organization=org, cluster=cluster, hostname=f"c{c}-node-{n}",
                gpu_type="NVIDIA A100", gpu_count=n_gpus, gpu_memory_gb=80,
                hourly_cost=8.50, status="active",
            )
            nodes.append(node)
            for g in range(n_gpus):
                gpu = GPU.objects_unscoped.create(
                    organization=org, node=node, gpu_index=g,
                    uuid=f"GPU-{org.slug}-c{c}n{n}g{g}",
                    current_utilization=50.0 + g, current_memory_used_mb=10000,
                    current_memory_total_mb=81920, current_temperature_c=60,
                    current_power_watts=200.0, status="healthy",
                )
                gpus.append(gpu)
    return clusters, nodes, gpus


class AuthAndScopeTest(TestCase):
    def setUp(self):
        self.user, self.org = _make_user_and_org("auth_org")
        self.client = Client()

    def test_missing_key_returns_401(self):
        r = self.client.get("/api/v1/gpus/")
        self.assertEqual(r.status_code, 401)
        self.assertIn("error", r.json())

    def test_invalid_key_returns_401(self):
        r = self.client.get("/api/v1/gpus/", HTTP_X_API_KEY="not-a-real-key")
        self.assertEqual(r.status_code, 401)

    def test_ingest_scope_cannot_read(self):
        _, raw = _make_ingest_key(self.org, self.user)
        r = self.client.get("/api/v1/gpus/", HTTP_X_API_KEY=raw)
        self.assertEqual(r.status_code, 403)
        self.assertIn("read", r.json()["error"])

    def test_read_scope_can_read(self):
        _, raw = _make_read_key(self.org, self.user)
        r = self.client.get("/api/v1/gpus/", HTTP_X_API_KEY=raw)
        self.assertEqual(r.status_code, 200)
        self.assertIn("results", r.json())


class TenantIsolationTest(TestCase):
    def setUp(self):
        self.user_a, self.org_a = _make_user_and_org("ti_a")
        self.user_b, self.org_b = _make_user_and_org("ti_b")
        _seed_minimal(self.org_a, n_clusters=1, n_nodes=2, n_gpus=3)
        _seed_minimal(self.org_b, n_clusters=1, n_nodes=1, n_gpus=2)
        _, self.raw_a = _make_read_key(self.org_a, self.user_a)
        _, self.raw_b = _make_read_key(self.org_b, self.user_b)
        self.client = Client()

    def test_clusters_isolated(self):
        ra = self.client.get("/api/v1/clusters/", HTTP_X_API_KEY=self.raw_a).json()
        rb = self.client.get("/api/v1/clusters/", HTTP_X_API_KEY=self.raw_b).json()
        self.assertEqual(len(ra["results"]), 1)
        self.assertEqual(len(rb["results"]), 1)
        self.assertNotEqual(ra["results"][0]["id"], rb["results"][0]["id"])

    def test_gpus_isolated(self):
        ra = self.client.get("/api/v1/gpus/", HTTP_X_API_KEY=self.raw_a).json()
        rb = self.client.get("/api/v1/gpus/", HTTP_X_API_KEY=self.raw_b).json()
        # Org A: 1 cluster × 2 nodes × 3 GPUs = 6
        self.assertEqual(len(ra["results"]), 6)
        # Org B: 1 cluster × 1 node × 2 GPUs = 2
        self.assertEqual(len(rb["results"]), 2)
        a_uuids = {g["uuid"] for g in ra["results"]}
        b_uuids = {g["uuid"] for g in rb["results"]}
        self.assertFalse(a_uuids & b_uuids, "Cross-org leak detected")

    def test_gpu_detail_cross_org_404(self):
        # Org A's GPU should be invisible to Org B's key
        a_uuid = self.client.get(
            "/api/v1/gpus/", HTTP_X_API_KEY=self.raw_a
        ).json()["results"][0]["uuid"]
        r = self.client.get(f"/api/v1/gpus/{a_uuid}/", HTTP_X_API_KEY=self.raw_b)
        self.assertEqual(r.status_code, 404)


class PaginationTest(TestCase):
    def setUp(self):
        self.user, self.org = _make_user_and_org("pag_org")
        # 12 GPUs (1 cluster × 2 nodes × 6 GPUs)
        _seed_minimal(self.org, n_clusters=1, n_nodes=2, n_gpus=6)
        _, self.raw = _make_read_key(self.org, self.user)
        self.client = Client()

    def test_default_limit_returns_all_when_under_limit(self):
        r = self.client.get("/api/v1/gpus/", HTTP_X_API_KEY=self.raw).json()
        self.assertEqual(len(r["results"]), 12)
        self.assertIsNone(r["next_cursor"])

    def test_custom_limit_caps_results(self):
        r = self.client.get("/api/v1/gpus/?limit=5", HTTP_X_API_KEY=self.raw).json()
        self.assertEqual(len(r["results"]), 5)
        self.assertIsNotNone(r["next_cursor"])
        self.assertEqual(r["limit"], 5)

    def test_cursor_pagination_round_trips(self):
        page1 = self.client.get("/api/v1/gpus/?limit=5", HTTP_X_API_KEY=self.raw).json()
        cursor = page1["next_cursor"]
        page2 = self.client.get(
            f"/api/v1/gpus/?limit=5&cursor={cursor}", HTTP_X_API_KEY=self.raw
        ).json()
        self.assertEqual(len(page2["results"]), 5)
        # No overlap
        ids1 = {g["uuid"] for g in page1["results"]}
        ids2 = {g["uuid"] for g in page2["results"]}
        self.assertFalse(ids1 & ids2)
        # Final page (last 2)
        page3 = self.client.get(
            f"/api/v1/gpus/?limit=5&cursor={page2['next_cursor']}",
            HTTP_X_API_KEY=self.raw,
        ).json()
        self.assertEqual(len(page3["results"]), 2)
        self.assertIsNone(page3["next_cursor"])

    def test_max_limit_enforced(self):
        r = self.client.get("/api/v1/gpus/?limit=99999", HTTP_X_API_KEY=self.raw).json()
        # Even though we have 12 GPUs, limit should be clamped to MAX (500)
        self.assertEqual(r["limit"], 500)

    def test_invalid_cursor_returns_400(self):
        r = self.client.get(
            "/api/v1/gpus/?cursor=not-valid-base64!@#",
            HTTP_X_API_KEY=self.raw,
        )
        self.assertEqual(r.status_code, 400)


class FilterTest(TestCase):
    def setUp(self):
        self.user, self.org = _make_user_and_org("filt_org")
        clusters, nodes, gpus = _seed_minimal(self.org, n_clusters=2, n_nodes=2, n_gpus=2)
        # Set one node to degraded
        nodes[0].status = "degraded"
        nodes[0].save()
        # Set one GPU to retired
        gpus[0].status = "retired"
        gpus[0].save()
        self.cluster_a = clusters[0]
        self.cluster_b = clusters[1]
        _, self.raw = _make_read_key(self.org, self.user)
        self.client = Client()

    def test_cluster_filter_by_cloud(self):
        r = self.client.get(
            "/api/v1/clusters/?cloud=aws", HTTP_X_API_KEY=self.raw
        ).json()
        self.assertTrue(all(c["cloud"] == "aws" for c in r["results"]))

    def test_node_filter_by_status(self):
        r = self.client.get(
            "/api/v1/nodes/?status=degraded", HTTP_X_API_KEY=self.raw
        ).json()
        self.assertEqual(len(r["results"]), 1)

    def test_gpu_filter_by_cluster(self):
        r = self.client.get(
            f"/api/v1/gpus/?cluster_id={self.cluster_a.id}",
            HTTP_X_API_KEY=self.raw,
        ).json()
        # cluster_a has 2 nodes × 2 GPUs = 4
        self.assertEqual(len(r["results"]), 4)


class CostsSummaryTest(TestCase):
    def setUp(self):
        self.user, self.org = _make_user_and_org("cs_org")
        _seed_minimal(self.org)
        _, self.raw = _make_read_key(self.org, self.user)
        self.client = Client()

    def test_costs_summary_empty_returns_zeros(self):
        r = self.client.get(
            "/api/v1/costs/summary/?hours=1", HTTP_X_API_KEY=self.raw
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("total_cost", body)
        self.assertIn("by_model", body)
        self.assertIn("by_node", body)
        self.assertIn("fleet_cost_per_hour", body)
        self.assertEqual(body["period_hours"], 1)

    def test_invalid_hours_returns_400(self):
        r = self.client.get(
            "/api/v1/costs/summary/?hours=99999", HTTP_X_API_KEY=self.raw
        )
        self.assertEqual(r.status_code, 400)


class AlertsApiTest(TestCase):
    def setUp(self):
        self.user, self.org = _make_user_and_org("ar_org")
        rule = AlertRule.objects.create(
            organization=self.org, name="r1", metric="latency_high",
            threshold_value=500.0, duration_seconds=300, is_enabled=True,
        )
        rule_disabled = AlertRule.objects.create(
            organization=self.org, name="r2", metric="gpu_utilization_low",
            threshold_value=20.0, duration_seconds=600, is_enabled=False,
        )
        AlertEvent.objects.create(rule=rule, severity="warning", message="m1")
        ev2 = AlertEvent.objects.create(rule=rule, severity="critical", message="m2")
        # Mark m2 resolved
        ev2.resolved_at = ev2.triggered_at
        ev2.save()
        _, self.raw = _make_read_key(self.org, self.user)
        self.client = Client()

    def test_rules_list(self):
        r = self.client.get(
            "/api/v1/alerts/rules/", HTTP_X_API_KEY=self.raw
        ).json()
        self.assertEqual(len(r["results"]), 2)

    def test_rules_filter_enabled(self):
        r = self.client.get(
            "/api/v1/alerts/rules/?enabled=true", HTTP_X_API_KEY=self.raw
        ).json()
        self.assertEqual(len(r["results"]), 1)

    def test_events_filter_active(self):
        r = self.client.get(
            "/api/v1/alerts/events/?status=active", HTTP_X_API_KEY=self.raw
        ).json()
        self.assertEqual(len(r["results"]), 1)
        self.assertTrue(r["results"][0]["is_active"])

    def test_events_filter_resolved(self):
        r = self.client.get(
            "/api/v1/alerts/events/?status=resolved", HTTP_X_API_KEY=self.raw
        ).json()
        self.assertEqual(len(r["results"]), 1)
        self.assertFalse(r["results"][0]["is_active"])

    def test_events_invalid_since_returns_400(self):
        r = self.client.get(
            "/api/v1/alerts/events/?since=not-a-date", HTTP_X_API_KEY=self.raw
        )
        self.assertEqual(r.status_code, 400)


class InferenceEndpointsTest(TestCase):
    def setUp(self):
        self.user, self.org = _make_user_and_org("ie_org")
        InferenceEndpoint.objects_unscoped.create(
            organization=self.org, name="ep1", engine="vllm",
            current_model="llama-3", status="serving",
        )
        InferenceEndpoint.objects_unscoped.create(
            organization=self.org, name="ep2", engine="tgi",
            current_model="mistral", status="idle",
        )
        _, self.raw = _make_read_key(self.org, self.user)
        self.client = Client()

    def test_list_endpoints(self):
        r = self.client.get(
            "/api/v1/inference/endpoints/", HTTP_X_API_KEY=self.raw
        ).json()
        self.assertEqual(len(r["results"]), 2)

    def test_filter_by_engine(self):
        r = self.client.get(
            "/api/v1/inference/endpoints/?engine=vllm", HTTP_X_API_KEY=self.raw
        ).json()
        self.assertEqual(len(r["results"]), 1)
        self.assertEqual(r["results"][0]["engine"], "vllm")
