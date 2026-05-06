"""
monitor/services/demo_seeder.py

Reusable demo-data seeder. Builds a synthetic GPU fleet (cluster + nodes + GPUs +
inference endpoints + alert rules + 30 days of TimescaleDB hypertable rows) into
any organization. Idempotent.

Two callers:
  - monitor/management/commands/seed_demo_data.py  (legacy CLI, demo-org)
  - monitor/views/onboarding_views.py:enable_demo_fleet  (logged-in user's org)
"""
import math
import random
import uuid
from datetime import timedelta
from typing import Callable

from django.db import connection
from django.utils import timezone

from monitor.models import (
    GPU,
    GPUCluster,
    GPUNode,
    GPUPricing,
    InferenceEndpoint,
    AlertRule,
    AlertEvent,
)


DEFAULT_CLUSTER_NAME = "Demo Fleet"

MODELS = [
    "Llama-3.1-70B",
    "Mistral-7B",
    "Qwen2.5-72B",
    "DeepSeek-V3",
]

GPU_TYPES = [
    ("NVIDIA H100-SXM5-80GB", 80),
    ("NVIDIA A100-SXM4-80GB", 80),
    ("NVIDIA A100-SXM4-40GB", 40),
    ("NVIDIA H100-PCIe-80GB", 80),
]

INSTANCE_TYPES = [
    ("p4d.24xlarge", 32.77),
    ("p3.16xlarge", 24.48),
    ("a3-highgpu-8g", 29.39),
    ("Standard_ND96asr_v4", 27.20),
]

INFERENCE_ENDPOINTS = [
    {
        "name": "llama-70b-prod",
        "engine": "vllm",
        "model": "meta-llama/Llama-3.1-70B-Instruct",
        "base_rps": 18.0,
        "base_tps": 2200.0,
        "base_latency": 95.0,
        "base_kv_cache": 72.0,
        "status": "serving",
    },
    {
        "name": "mistral-7b-fast",
        "engine": "vllm",
        "model": "mistralai/Mistral-7B-Instruct-v0.3",
        "base_rps": 45.0,
        "base_tps": 5800.0,
        "base_latency": 35.0,
        "base_kv_cache": 48.0,
        "status": "serving",
    },
    {
        "name": "qwen-72b-batch",
        "engine": "tgi",
        "model": "Qwen/Qwen2.5-72B-Instruct",
        "base_rps": 8.0,
        "base_tps": 1400.0,
        "base_latency": 210.0,
        "base_kv_cache": 85.0,
        "status": "serving",
    },
    {
        "name": "deepseek-v3-exp",
        "engine": "ollama",
        "model": "deepseek-ai/DeepSeek-V3",
        "base_rps": 2.0,
        "base_tps": 380.0,
        "base_latency": 480.0,
        "base_kv_cache": 30.0,
        "status": "idle",
    },
]

GPU_PRICING = [
    {"pattern": "H100", "rate": "12.2900", "provider": "CoreWeave"},
    {"pattern": "A100", "rate": "8.5000", "provider": "AWS"},
    {"pattern": "A10G", "rate": "3.5000", "provider": "AWS"},
    {"pattern": "RTX 4090", "rate": "2.2000", "provider": "Lambda Labs"},
]


def _business_hour_factor(dt) -> float:
    """Utilization multiplier — higher 08:00–20:00 UTC, lower at night."""
    hour = dt.hour + dt.minute / 60.0
    angle = math.pi * (hour - 2) / 12
    return 0.40 + 0.60 * max(0.0, math.sin(angle))


def _noop(msg: str) -> None:
    pass


def seed_demo_fleet(
    org,
    user=None,
    nodes: int = 4,
    gpus_per_node: int = 4,
    hours: int = 720,
    cluster_name: str = DEFAULT_CLUSTER_NAME,
    *,
    log: Callable[[str], None] = _noop,
) -> dict:
    """
    Seed a demo GPU fleet into *org*. Idempotent — safe to run repeatedly.

    Args:
        org: Organization to seed into.
        user: Unused (kept for API symmetry with the CLI's demo-user creation).
        nodes: Number of GPU nodes (1–32).
        gpus_per_node: GPUs per node (1–16).
        hours: Hours of historical hypertable data to backfill (1–720, default 30 days).
        cluster_name: Name for the demo cluster. Defaults to "Demo Fleet".
        log: Optional callback for progress strings (CLI passes self.stdout.write).

    Returns:
        dict with row counts: cluster_id, gpus, nodes, endpoints, gpu_metrics_rows,
        inference_metrics_rows, cost_snapshot_rows, alert_rules.
    """
    if not (1 <= nodes <= 32):
        raise ValueError("nodes must be between 1 and 32")
    if not (1 <= gpus_per_node <= 16):
        raise ValueError("gpus_per_node must be between 1 and 16")
    if not (1 <= hours <= 720):
        raise ValueError("hours must be between 1 and 720")

    log(f"  nodes={nodes}  gpus/node={gpus_per_node}  hours={hours}  cluster={cluster_name}")

    # ── 1. Cluster ────────────────────────────────────────────────────────────
    cluster, cluster_created = GPUCluster.objects_unscoped.get_or_create(
        organization=org,
        name=cluster_name,
        defaults={
            "cloud": "aws",
            "region": "us-east-1",
            "k8s_context": "demo-k8s",
        },
    )
    log(f"  Cluster: {cluster.name} ({'created' if cluster_created else 'existing'})")

    # ── 2. Nodes + GPUs ───────────────────────────────────────────────────────
    gpu_type_name, vram_gb = random.choice(GPU_TYPES)
    instance_type, hourly_cost = random.choice(INSTANCE_TYPES)
    memory_total_mb = vram_gb * 1024

    node_objs = []
    all_gpus = []

    for n in range(nodes):
        hostname = f"demo-node-{n:02d}"
        node, _ = GPUNode.objects_unscoped.get_or_create(
            organization=org,
            hostname=hostname,
            defaults={
                "cluster": cluster,
                "instance_type": instance_type,
                "gpu_count": gpus_per_node,
                "gpu_type": gpu_type_name,
                "gpu_memory_gb": vram_gb,
                "hourly_cost": round(hourly_cost + random.uniform(-2, 2), 4),
                "status": "active",
            },
        )
        node_objs.append(node)

        for g in range(gpus_per_node):
            base_util = random.uniform(20, 85)
            gpu_uuid = f"GPU-demo-{org.slug}-{n:02d}-{g:02d}-{uuid.uuid4().hex[:8]}"
            gpu, _ = GPU.objects_unscoped.get_or_create(
                node=node,
                gpu_index=g,
                defaults={
                    "organization": org,
                    "uuid": gpu_uuid,
                    "current_utilization": base_util,
                    "current_memory_used_mb": int(memory_total_mb * base_util / 100),
                    "current_memory_total_mb": memory_total_mb,
                    "current_temperature_c": int(30 + base_util * 0.6),
                    "current_power_watts": round(50 + base_util * 3.5, 1),
                    "current_model_name": random.choice(MODELS),
                    "status": "healthy",
                },
            )
            all_gpus.append((gpu.uuid, node.hostname, g, gpu.current_utilization, gpu.current_model_name))

    total_gpus = len(all_gpus)
    log(f"  {len(node_objs)} nodes × {gpus_per_node} GPUs = {total_gpus} GPU records")

    # ── 3. GPU pricing (global table) ─────────────────────────────────────────
    for p in GPU_PRICING:
        GPUPricing.objects.get_or_create(
            gpu_model_pattern=p["pattern"],
            pricing_type="on_demand",
            defaults={
                "hourly_rate": p["rate"],
                "provider": p["provider"],
            },
        )

    # ── 4. Inference endpoints ────────────────────────────────────────────────
    endpoints_created = []
    for ep_cfg in INFERENCE_ENDPOINTS:
        ep, _ = InferenceEndpoint.objects_unscoped.get_or_create(
            organization=org,
            name=ep_cfg["name"],
            defaults={
                "engine": ep_cfg["engine"],
                "current_model": ep_cfg["model"],
                "status": ep_cfg["status"],
                "is_active": True,
                "url": f"http://localhost:8{8000 + INFERENCE_ENDPOINTS.index(ep_cfg)}/v1",
                "current_requests_per_sec": ep_cfg["base_rps"],
                "current_tokens_per_sec": ep_cfg["base_tps"],
                "current_avg_latency_ms": ep_cfg["base_latency"],
                "current_p99_latency_ms": ep_cfg["base_latency"] * 4.5,
                "current_queue_depth": random.randint(0, 8),
                "current_kv_cache_usage_pct": ep_cfg["base_kv_cache"],
                "current_batch_utilization": round(random.uniform(4, 16), 1),
                "last_seen": timezone.now(),
            },
        )
        endpoints_created.append(ep)

    # ── 5. Hypertable backfills ───────────────────────────────────────────────
    now = timezone.now().replace(second=0, microsecond=0)
    total_minutes = hours * 60
    batch_size = 10_000

    gpu_metric_rows = _backfill_gpu_metrics(
        all_gpus, memory_total_mb, total_minutes, now, batch_size, log,
    )

    inference_metric_rows = _backfill_inference_metrics(
        endpoints_created, total_minutes, now, batch_size, log,
    )

    cost_snapshot_rows = _backfill_cost_snapshots(
        all_gpus, gpu_type_name, total_minutes, now, batch_size, log,
    )

    # ── 6. Alert rules + sample events ────────────────────────────────────────
    rule1, _ = AlertRule.objects.get_or_create(
        organization=org, name="GPU Underutilization Alert",
        defaults={"metric": "gpu_utilization_low", "threshold_value": 20.0,
                  "duration_seconds": 600, "is_enabled": True},
    )
    rule2, _ = AlertRule.objects.get_or_create(
        organization=org, name="High Inference Latency",
        defaults={"metric": "latency_high", "threshold_value": 500.0,
                  "duration_seconds": 300, "is_enabled": True},
    )
    rule3, _ = AlertRule.objects.get_or_create(
        organization=org, name="GPU Memory Pressure",
        defaults={"metric": "gpu_memory_high", "threshold_value": 90.0,
                  "duration_seconds": 180, "is_enabled": True},
    )

    if not AlertEvent.objects.filter(rule__organization=org).exists():
        AlertEvent.objects.create(
            rule=rule1, severity="warning",
            message="GPU util fell below 20% on demo-node-02 for 10+ minutes",
            context={"node": "demo-node-02", "utilization": 14.2},
            notification_sent=False,
        )
        AlertEvent.objects.create(
            rule=rule2, severity="warning",
            message="qwen-72b-batch p99 latency exceeded 500ms threshold",
            context={"endpoint": "qwen-72b-batch", "latency_p99": 720.0},
            notification_sent=True,
            resolved_at=now - timedelta(hours=1),
        )
        AlertEvent.objects.create(
            rule=rule3, severity="critical",
            message="GPU memory usage reached 93% on demo-node-00:GPU0",
            context={"node": "demo-node-00", "gpu_index": 0, "memory_pct": 93.1},
            notification_sent=True,
            resolved_at=now - timedelta(minutes=30),
        )

    log("  Done.")

    return {
        "cluster_id": str(cluster.id),
        "cluster_name": cluster.name,
        "nodes": len(node_objs),
        "gpus": total_gpus,
        "endpoints": len(endpoints_created),
        "gpu_metrics_rows": gpu_metric_rows,
        "inference_metrics_rows": inference_metric_rows,
        "cost_snapshot_rows": cost_snapshot_rows,
        "alert_rules": 3,
        "hours": hours,
    }


# ── Hypertable backfill helpers ───────────────────────────────────────────────

_GPU_INSERT_SQL = """
    INSERT INTO gpu_metrics (
        time, gpu_uuid, node_name,
        utilization, memory_used_mb, memory_total_mb,
        temperature, power_watts, sm_clock_mhz, mem_clock_mhz,
        pcie_tx_bytes, pcie_rx_bytes, ecc_single, ecc_double
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT DO NOTHING
"""

_INF_INSERT_SQL = """
    INSERT INTO inference_metrics (
        time, endpoint_id, model_name,
        requests_running, requests_waiting,
        prompt_throughput, generation_throughput,
        gpu_cache_usage, cpu_cache_usage,
        latency_p50, latency_p95, latency_p99,
        ttft_p50, ttft_p95, ttft_p99,
        tpot_avg, preemptions_total, batch_size_avg
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    ON CONFLICT DO NOTHING
"""

_COST_INSERT_SQL = """
    INSERT INTO cost_snapshots (
        time, gpu_uuid, endpoint_id,
        model_name, hourly_rate,
        utilization, cost_this_period, waste_this_period
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT DO NOTHING
"""


def _backfill_gpu_metrics(all_gpus, memory_total_mb, total_minutes, now, batch_size, log):
    rows_buffer = []
    total_inserted = 0

    for minute_offset in range(total_minutes, 0, -1):
        ts = now - timedelta(minutes=minute_offset)
        bh = _business_hour_factor(ts)
        ts_str = ts.isoformat()

        for gpu_uuid, node_name, _gpu_index, base_util, _model_name in all_gpus:
            util = max(0.0, min(100.0, base_util * bh + random.gauss(0, 5)))
            mem_used = int(memory_total_mb * util / 100 * random.uniform(0.9, 1.1))
            mem_used = max(512, min(memory_total_mb, mem_used))
            temp = int(28 + util * 0.62 + random.uniform(-2, 2))
            power = round(45 + util * 3.6 + random.gauss(0, 8), 1)
            power = max(30.0, min(400.0, power))
            sm_clock = int(900 + util * 4.5)
            mem_clock = random.choice([877, 1215, 1593])

            rows_buffer.append((
                ts_str, gpu_uuid, node_name,
                round(util, 2), mem_used, memory_total_mb,
                temp, power, sm_clock, mem_clock,
                random.randint(0, 200_000_000),
                random.randint(0, 100_000_000),
                0, 0,
            ))

            if len(rows_buffer) >= batch_size:
                with connection.cursor() as cur:
                    cur.executemany(_GPU_INSERT_SQL, rows_buffer)
                total_inserted += len(rows_buffer)
                rows_buffer = []

    if rows_buffer:
        with connection.cursor() as cur:
            cur.executemany(_GPU_INSERT_SQL, rows_buffer)
        total_inserted += len(rows_buffer)

    log(f"  Inserted {total_inserted:,} GPU metric rows")
    return total_inserted


def _backfill_inference_metrics(endpoints_created, total_minutes, now, batch_size, log):
    inf_rows = []
    inf_total = 0

    for ep, ep_cfg in zip(endpoints_created, INFERENCE_ENDPOINTS):
        ep_int_id = abs(hash(str(ep.pk))) % (2 ** 31)
        for minute_offset in range(total_minutes, 0, -1):
            ts = now - timedelta(minutes=minute_offset)
            bh = _business_hour_factor(ts)
            ts_str = ts.isoformat()

            rps = max(0, ep_cfg["base_rps"] * bh + random.gauss(0, ep_cfg["base_rps"] * 0.1))
            tps = max(0, ep_cfg["base_tps"] * bh + random.gauss(0, ep_cfg["base_tps"] * 0.08))
            lat = max(5, ep_cfg["base_latency"] + random.gauss(0, ep_cfg["base_latency"] * 0.15))
            kv = max(0, min(100, ep_cfg["base_kv_cache"] + random.gauss(0, 5)))

            inf_rows.append((
                ts_str, ep_int_id, ep_cfg["model"],
                int(rps * 2),
                max(0, int(rps * 0.3)),
                tps * 0.25,
                tps,
                kv / 100.0,
                random.uniform(0.01, 0.1),
                round(lat, 1),
                round(lat * 2.5, 1),
                round(lat * 5.0, 1),
                round(lat * 0.3, 1),
                round(lat * 0.8, 1),
                round(lat * 1.5, 1),
                round(random.uniform(3, 8), 2),
                0,
                round(random.uniform(4, 16), 1),
            ))

            if len(inf_rows) >= batch_size:
                with connection.cursor() as cur:
                    cur.executemany(_INF_INSERT_SQL, inf_rows)
                inf_total += len(inf_rows)
                inf_rows = []

    if inf_rows:
        with connection.cursor() as cur:
            cur.executemany(_INF_INSERT_SQL, inf_rows)
        inf_total += len(inf_rows)

    log(f"  Inserted {inf_total:,} inference metric rows")
    return inf_total


def _backfill_cost_snapshots(all_gpus, gpu_type_name, total_minutes, now, batch_size, log):
    pricing_entries = list(GPUPricing.objects.all())

    def _match_rate(model_str):
        model_lower = (model_str or "").lower()
        for p in pricing_entries:
            if p.gpu_model_pattern.lower() in model_lower:
                return float(p.hourly_rate)
        return 8.50

    cost_rows = []
    cost_total = 0
    interval_secs = 60.0

    for minute_offset in range(total_minutes, 0, -1):
        ts = now - timedelta(minutes=minute_offset)
        bh = _business_hour_factor(ts)
        ts_str = ts.isoformat()

        for gpu_uuid, _node_name, _gpu_index, base_util, model_name in all_gpus:
            util = max(0.0, min(100.0, base_util * bh + random.gauss(0, 5)))
            rate = _match_rate(gpu_type_name)
            cost = rate * (interval_secs / 3600.0)
            waste = cost * (1.0 - util / 100.0)

            cost_rows.append((
                ts_str, gpu_uuid, None,
                model_name, rate,
                round(util, 2),
                round(cost, 8), round(waste, 8),
            ))

            if len(cost_rows) >= batch_size:
                with connection.cursor() as cur:
                    cur.executemany(_COST_INSERT_SQL, cost_rows)
                cost_total += len(cost_rows)
                cost_rows = []

    if cost_rows:
        with connection.cursor() as cur:
            cur.executemany(_COST_INSERT_SQL, cost_rows)
        cost_total += len(cost_rows)

    log(f"  Inserted {cost_total:,} cost snapshot rows")
    return cost_total
