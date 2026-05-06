"""
monitor/tests/test_chart_endpoints.py

Tests for /charts/* JSON endpoints (session-authenticated, Chart.js-shaped).
"""
from django.contrib.auth.models import User
from django.test import Client, TestCase

from monitor.models import GPU, GPUCluster, GPUNode, InferenceEndpoint, Organization


CHART_URLS = [
    "/charts/gpu-util-timeseries/",
    "/charts/cost-trend/",
    "/charts/alert-timeline/",
    "/charts/inference-latency/",
]


def _make_user_and_org(slug):
    user = User.objects.create_user(username=slug, password="pw")
    org = Organization.objects.create(name=slug, slug=slug, owner=user)
    profile = user.profile
    profile.organization = org
    profile.role = "owner"
    profile.save()
    return user, org


class ChartAuthTest(TestCase):
    def test_login_required_for_all(self):
        client = Client()
        for url in CHART_URLS:
            r = client.get(url)
            self.assertIn(r.status_code, (302, 401), f"{url} should require auth")


class ChartShapeTest(TestCase):
    def setUp(self):
        self.user, self.org = _make_user_and_org("ch_org")
        self.client = Client()
        self.client.login(username="ch_org", password="pw")

    def test_empty_org_returns_chart_shape(self):
        # No GPUs yet — endpoints must still return valid Chart.js JSON
        for url in CHART_URLS:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, f"{url} returned {r.status_code}")
            body = r.json()
            self.assertIn("labels", body)
            self.assertIn("datasets", body)
            self.assertIsInstance(body["labels"], list)
            self.assertIsInstance(body["datasets"], list)

    def test_gpu_util_dataset_label(self):
        cluster = GPUCluster.objects_unscoped.create(
            organization=self.org, name="c1", cloud="aws",
        )
        node = GPUNode.objects_unscoped.create(
            organization=self.org, cluster=cluster, hostname="n1",
            gpu_type="H100", gpu_count=1, gpu_memory_gb=80, status="active",
        )
        GPU.objects_unscoped.create(
            organization=self.org, node=node, gpu_index=0, uuid="GPU-test-1",
            current_utilization=50.0, current_memory_total_mb=81920, status="healthy",
        )
        r = self.client.get("/charts/gpu-util-timeseries/")
        body = r.json()
        self.assertEqual(body["datasets"][0]["label"], "Avg Utilization %")

    def test_alert_timeline_returns_three_severity_series(self):
        r = self.client.get("/charts/alert-timeline/")
        body = r.json()
        labels = [d["label"] for d in body["datasets"]]
        self.assertIn("Critical", labels)
        self.assertIn("Warning", labels)
        self.assertIn("Info", labels)


class ChartTenantIsolationTest(TestCase):
    def setUp(self):
        self.user_a, self.org_a = _make_user_and_org("cti_a")
        self.user_b, self.org_b = _make_user_and_org("cti_b")
        # Org A has 1 endpoint, Org B has 0
        InferenceEndpoint.objects_unscoped.create(
            organization=self.org_a, name="ep-a", engine="vllm",
            current_model="llama", status="serving",
        )

    def test_user_b_cannot_see_user_a_endpoints(self):
        client = Client()
        client.login(username="cti_b", password="pw")
        r = client.get("/charts/inference-latency/")
        body = r.json()
        # Org B has no endpoints, so labels/datasets must be empty
        self.assertEqual(body["labels"], [])
        self.assertEqual(body["datasets"], [])

    def test_user_a_sees_their_endpoint(self):
        client = Client()
        client.login(username="cti_a", password="pw")
        r = client.get("/charts/inference-latency/")
        body = r.json()
        self.assertEqual(body["labels"], ["ep-a"])
        # 3 series: p50/p95/p99
        self.assertEqual(len(body["datasets"]), 3)
