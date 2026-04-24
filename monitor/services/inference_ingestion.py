"""
monitor/services/inference_ingestion.py

Inference metric ingestion service.

Receives a parsed payload from the vLLM / TGI scraper (or from the
/api/v1/ingest/inference/ REST endpoint) and:

  1. Upserts the InferenceEndpoint record (creating it if unknown).
  2. Updates the endpoint's denormalized current_* snapshot fields.
  3. Bulk-inserts one row into the inference_metrics hypertable.

Payload format:
{
    "endpoint_name": "llama-70b",
    "model_name":    "meta-llama/Llama-3.1-70B",
    "engine":        "vllm",    # optional, defaults to "vllm"
    "url":           "http://...",  # optional
    "metrics": {
        "requests_running":       12,
        "requests_waiting":        3,
        "prompt_throughput":     450.0,
        "generation_throughput": 1800.0,
        "gpu_cache_usage":       0.72,
        "cpu_cache_usage":       0.10,
        "latency_p50":           95.0,
        "latency_p95":          320.0,
        "latency_p99":          680.0,
        "ttft_p50":              40.0,
        "ttft_p95":             120.0,
        "ttft_p99":             280.0,
        "tpot_avg":               4.5,
        "preemptions_total":       0,
        "batch_size_avg":          8.2
    }
}
"""
import hashlib
import logging

from django.db import connection, transaction
from django.utils import timezone as django_tz

from monitor.models import InferenceEndpoint

logger = logging.getLogger(__name__)


def ingest_inference_metrics(organization, payload: dict) -> int:
    """
    Process one inference-metrics payload for *organization*.

    Returns 1 if a hypertable row was inserted, 0 otherwise.
    """
    endpoint_name: str = payload.get("endpoint_name", "")
    model_name: str = payload.get("model_name", "")
    engine: str = payload.get("engine", "vllm")
    url: str = payload.get("url", "")
    metrics: dict = payload.get("metrics", {})

    if not endpoint_name:
        raise ValueError("payload must include 'endpoint_name'")

    now = django_tz.now()

    with transaction.atomic():
        # ── 1. Upsert InferenceEndpoint ───────────────────────────────────────
        endpoint, _ = InferenceEndpoint.objects_unscoped.update_or_create(
            organization=organization,
            name=endpoint_name,
            defaults={
                "engine": engine,
                "url": url,
                "current_model": model_name,
                "status": "serving",
                "is_active": True,
                "last_seen": now,
                # Current snapshot from this sample
                "current_requests_per_sec": _derive_req_per_sec(metrics),
                "current_tokens_per_sec": metrics.get("generation_throughput"),
                "current_avg_latency_ms": metrics.get("latency_p50"),
                "current_p99_latency_ms": metrics.get("latency_p99"),
                "current_queue_depth": metrics.get("requests_waiting"),
                "current_kv_cache_usage_pct": _pct(metrics.get("gpu_cache_usage")),
                "current_batch_utilization": metrics.get("batch_size_avg"),
            },
        )

        # ── 2. Insert into inference_metrics hypertable ──────────────────────
        insert_sql = """
            INSERT INTO inference_metrics (
                time, endpoint_id,
                model_name,
                requests_running, requests_waiting,
                prompt_throughput, generation_throughput,
                gpu_cache_usage, cpu_cache_usage,
                latency_p50, latency_p95, latency_p99,
                ttft_p50, ttft_p95, ttft_p99,
                tpot_avg, preemptions_total, batch_size_avg
            ) VALUES (
                %s, %s,
                %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s
            )
        """

        # endpoint_id stored as integer in hypertable; use the surrogate int PK
        # if UUID is the primary key — we need a stable integer reference.
        endpoint_int_id = _endpoint_int_id(endpoint)

        ts = now.isoformat()
        row = (
            ts,
            endpoint_int_id,
            model_name or None,
            metrics.get("requests_running"),
            metrics.get("requests_waiting"),
            metrics.get("prompt_throughput"),
            metrics.get("generation_throughput"),
            metrics.get("gpu_cache_usage"),
            metrics.get("cpu_cache_usage"),
            metrics.get("latency_p50"),
            metrics.get("latency_p95"),
            metrics.get("latency_p99"),
            metrics.get("ttft_p50"),
            metrics.get("ttft_p95"),
            metrics.get("ttft_p99"),
            metrics.get("tpot_avg"),
            metrics.get("preemptions_total"),
            metrics.get("batch_size_avg"),
        )

        with connection.cursor() as cur:
            cur.execute(insert_sql, row)

    logger.info(
        "Ingested inference metrics for endpoint=%s model=%s org=%s",
        endpoint_name,
        model_name,
        organization.slug,
    )
    return 1


# ── Helpers ───────────────────────────────────────────────────────────────────

def _derive_req_per_sec(metrics: dict):
    """Estimate requests/sec from prompt_throughput if not directly provided."""
    rps = metrics.get("requests_per_sec")
    if rps is not None:
        return rps
    # rough approximation: prompt throughput / avg prompt length (256 tokens)
    pt = metrics.get("prompt_throughput")
    if pt is not None:
        return round(pt / 256.0, 3)
    return None


def _pct(value):
    """Convert 0-1 fraction to 0-100 percent, or return as-is if already >1."""
    if value is None:
        return None
    if value <= 1.0:
        return round(value * 100.0, 2)
    return value


def _endpoint_int_id(endpoint) -> int:
    """
    The inference_metrics hypertable uses INTEGER endpoint_id.
    InferenceEndpoint uses a UUID PK; we use a stable hash truncated to
    a positive 32-bit integer so we can round-trip queries.
    In practice callers should join on the Django table; this is just a
    stable foreign-key surrogate for the hypertable column.
    """
    # Use a deterministic hash (md5) so the ID is stable across Python restarts
    digest = hashlib.md5(str(endpoint.pk).encode()).hexdigest()
    return int(digest[:8], 16) % (2 ** 31)
