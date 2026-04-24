"""
monitor/services/metric_ingestion.py

Metric ingestion service: upserts GPU topology records and bulk-inserts raw
telemetry rows into the gpu_metrics TimescaleDB hypertable.
"""
import logging
from datetime import datetime, timezone

from django.db import connection, transaction
from django.utils import timezone as django_tz

from monitor.models import GPU, GPUNode

logger = logging.getLogger(__name__)


def ingest_gpu_metrics(organization, cluster, payload: dict) -> int:
    """
    Process a single agent payload and persist metrics.

    Steps:
      1. Upsert the GPUNode identified by (cluster, hostname).
      2. Upsert each GPU device identified by uuid.
      3. Update the GPU's denormalized current_* snapshot fields.
      4. Bulk-insert one row per GPU metric into the gpu_metrics hypertable.

    Returns the number of metric rows inserted.
    """
    node_name: str = payload["node_name"]
    gpu_type: str = payload.get("gpu_type", "")
    metrics: list = payload.get("metrics", [])

    if not metrics:
        return 0

    with transaction.atomic():
        # ── 1. Upsert GPUNode ─────────────────────────────────────────────────
        node, _ = GPUNode.objects.update_or_create(
            cluster=cluster,
            organization=organization,
            hostname=node_name,
            defaults={
                "gpu_type": gpu_type,
                "gpu_count": len(metrics),
                "status": "active",
                "last_seen": django_tz.now(),
            },
        )

        # ── 2 & 3. Upsert GPU devices + update current_* snapshot ────────────
        now = django_tz.now()
        for m in metrics:
            gpu_uuid: str = m["gpu_uuid"]
            gpu_index: int = m.get("gpu_index", 0)

            gpu, _ = GPU.objects.update_or_create(
                uuid=gpu_uuid,
                defaults={
                    "node": node,
                    "organization": organization,
                    "gpu_index": gpu_index,
                    "current_utilization": m.get("utilization"),
                    "current_memory_used_mb": m.get("memory_used_mb"),
                    "current_memory_total_mb": m.get("memory_total_mb"),
                    "current_temperature_c": m.get("temperature"),
                    "current_power_watts": m.get("power_watts"),
                    "current_clock_mhz": m.get("sm_clock_mhz"),
                    "last_updated": now,
                },
            )

        # ── 4. Bulk-insert into gpu_metrics hypertable ───────────────────────
        ts = now.isoformat()

        rows = [
            (
                ts,
                m["gpu_uuid"],
                node_name,
                m.get("utilization"),
                m.get("memory_used_mb"),
                m.get("memory_total_mb"),
                m.get("temperature"),
                m.get("power_watts"),
                m.get("sm_clock_mhz"),
                m.get("mem_clock_mhz"),
                m.get("pcie_tx_bytes", 0),
                m.get("pcie_rx_bytes", 0),
                m.get("ecc_single", 0),
                m.get("ecc_double", 0),
            )
            for m in metrics
        ]

        insert_sql = """
            INSERT INTO gpu_metrics (
                time, gpu_uuid, node_name,
                utilization, memory_used_mb, memory_total_mb,
                temperature, power_watts, sm_clock_mhz, mem_clock_mhz,
                pcie_tx_bytes, pcie_rx_bytes, ecc_single, ecc_double
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        with connection.cursor() as cur:
            cur.executemany(insert_sql, rows)

    logger.info(
        "Ingested %d metric rows for node %s (org=%s, cluster=%s)",
        len(rows),
        node_name,
        organization.slug,
        cluster.name,
    )
    return len(rows)
