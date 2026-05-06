"""
monitor/rest_api.py

Plain Django function-based REST API views.
No DRF required -- responses are plain JSON.

Two endpoint families:
  - Ingest (POST):  X-API-Key with 'ingest' scope. Used by agents.
  - Read   (GET):   X-API-Key with 'read'   scope. Used by external integrators.
"""
import json
import logging

from django.http import Http404, JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from monitor.api_auth import authenticate_api_key
from monitor.api_pagination import CursorError, paginate
from monitor.models import (
    GPU,
    GPUCluster,
    GPUNode,
    InferenceEndpoint,
    AlertRule,
    AlertEvent,
)
from monitor.services.cost_engine import get_cost_summary, get_fleet_cost_rate
from monitor.services.metric_ingestion import ingest_gpu_metrics
from monitor.services.inference_ingestion import ingest_inference_metrics

logger = logging.getLogger(__name__)


# ── Auth helper ───────────────────────────────────────────────────────────────

def _require_scope(request, scope):
    """Returns (api_key, None) on success, or (None, JsonResponse) on failure."""
    api_key, err = authenticate_api_key(request)
    if err:
        return None, JsonResponse({"error": err}, status=401)
    if scope not in (api_key.scopes or []):
        return None, JsonResponse({"error": f"API key lacks '{scope}' scope"}, status=403)
    return api_key, None


def _paginated_response(qs, request, serializer):
    try:
        items, next_cursor, limit = paginate(qs, request)
    except CursorError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({
        "results": [serializer(o) for o in items],
        "next_cursor": next_cursor,
        "limit": limit,
    })


# ── Ingest endpoint ───────────────────────────────────────────────────────────

@csrf_exempt
@require_POST
def ingest_gpu(request):
    """
    POST /api/v1/ingest/gpu/

    Accept a JSON payload from the GPU monitoring agent and persist metrics.

    Required header:
        X-API-Key: <key with 'ingest' scope>

    Request body (JSON):
        {
            "cluster_name": "my-cluster",
            "node_name": "gpu-node-01",
            "gpu_type": "H100-SXM",
            "metrics": [ { ... } ]
        }

    Response (200):
        { "status": "ok", "ingested": <int> }
    """
    # ── Auth ──────────────────────────────────────────────────────────────────
    api_key, err = authenticate_api_key(request)
    if err:
        return JsonResponse({"error": err}, status=401)

    if "ingest" not in (api_key.scopes or []):
        return JsonResponse({"error": "API key lacks 'ingest' scope"}, status=403)

    # ── Parse body ────────────────────────────────────────────────────────────
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, ValueError) as exc:
        return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)

    # ── Validate required fields ──────────────────────────────────────────────
    if not isinstance(payload.get("node_name"), str) or not payload["node_name"]:
        return JsonResponse({"error": "Missing or invalid 'node_name'"}, status=400)
    metrics = payload.get("metrics")
    if not isinstance(metrics, list):
        return JsonResponse({"error": "'metrics' must be a list"}, status=400)
    if len(metrics) > 1000:
        return JsonResponse({"error": "Too many metrics (max 1000)"}, status=400)
    for i, m in enumerate(metrics):
        if not isinstance(m, dict) or "gpu_uuid" not in m:
            return JsonResponse({"error": f"metrics[{i}] must be a dict with 'gpu_uuid'"}, status=400)

    cluster_name = payload.get("cluster_name", "default")
    organization = api_key.organization

    # ── Resolve / create cluster ──────────────────────────────────────────────
    cluster, _ = GPUCluster.objects.get_or_create(
        organization=organization,
        name=cluster_name,
        defaults={"cloud": "other"},
    )

    # ── Ingest ────────────────────────────────────────────────────────────────
    try:
        count = ingest_gpu_metrics(organization, cluster, payload)
    except Exception as exc:
        logger.exception("Metric ingestion failed: %s", exc)
        return JsonResponse({"error": "Ingestion failed"}, status=500)

    return JsonResponse({"status": "ok", "ingested": count})


# ── Read endpoints (GET, scope='read') ────────────────────────────────────────

def _ser_cluster(c):
    return {
        "id": str(c.id),
        "name": c.name,
        "cloud": c.cloud,
        "region": c.region,
        "is_active": c.is_active,
        "node_count": c.nodes.count(),
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _ser_node(n):
    return {
        "id": str(n.id),
        "cluster_id": str(n.cluster_id),
        "hostname": n.hostname,
        "instance_type": n.instance_type,
        "gpu_type": n.gpu_type,
        "gpu_count": n.gpu_count,
        "gpu_memory_gb": n.gpu_memory_gb,
        "hourly_cost": float(n.hourly_cost) if n.hourly_cost is not None else None,
        "status": n.status,
        "last_seen": n.last_seen.isoformat() if n.last_seen else None,
        "is_active": n.is_active,
    }


def _ser_gpu(g):
    return {
        "uuid": g.uuid,
        "id": str(g.id),
        "node_id": str(g.node_id),
        "gpu_index": g.gpu_index,
        "current_utilization": g.current_utilization,
        "current_memory_used_mb": g.current_memory_used_mb,
        "current_memory_total_mb": g.current_memory_total_mb,
        "current_temperature_c": g.current_temperature_c,
        "current_power_watts": g.current_power_watts,
        "current_model_name": g.current_model_name,
        "status": g.status,
        "ecc_errors": g.ecc_errors,
        "last_updated": g.last_updated.isoformat() if g.last_updated else None,
    }


def _ser_endpoint(e):
    return {
        "id": str(e.id),
        "name": e.name,
        "engine": e.engine,
        "url": e.url,
        "current_model": e.current_model,
        "current_requests_per_sec": e.current_requests_per_sec,
        "current_tokens_per_sec": e.current_tokens_per_sec,
        "current_avg_latency_ms": e.current_avg_latency_ms,
        "current_p99_latency_ms": e.current_p99_latency_ms,
        "current_kv_cache_usage_pct": e.current_kv_cache_usage_pct,
        "status": e.status,
        "last_seen": e.last_seen.isoformat() if e.last_seen else None,
        "is_active": e.is_active,
    }


def _ser_alert_rule(r):
    return {
        "id": r.id,
        "name": r.name,
        "metric": r.metric,
        "threshold_value": r.threshold_value,
        "duration_seconds": r.duration_seconds,
        "is_enabled": r.is_enabled,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _ser_alert_event(e):
    return {
        "id": e.id,
        "rule_id": e.rule_id,
        "triggered_at": e.triggered_at.isoformat() if e.triggered_at else None,
        "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
        "severity": e.severity,
        "message": e.message,
        "context": e.context,
        "is_active": e.resolved_at is None,
    }


@require_GET
def list_clusters(request):
    """GET /api/v1/clusters/?cloud=&limit=&cursor="""
    api_key, err = _require_scope(request, "read")
    if err:
        return err
    qs = GPUCluster.objects_unscoped.filter(organization=api_key.organization).order_by("id")
    cloud = request.GET.get("cloud")
    if cloud:
        qs = qs.filter(cloud=cloud)
    return _paginated_response(qs, request, _ser_cluster)


@require_GET
def list_nodes(request):
    """GET /api/v1/nodes/?cluster_id=&status=&limit=&cursor="""
    api_key, err = _require_scope(request, "read")
    if err:
        return err
    qs = GPUNode.objects_unscoped.filter(organization=api_key.organization).order_by("id")
    if cluster_id := request.GET.get("cluster_id"):
        qs = qs.filter(cluster_id=cluster_id)
    if status := request.GET.get("status"):
        qs = qs.filter(status=status)
    return _paginated_response(qs, request, _ser_node)


@require_GET
def list_gpus(request):
    """GET /api/v1/gpus/?node_id=&cluster_id=&status=&limit=&cursor="""
    api_key, err = _require_scope(request, "read")
    if err:
        return err
    qs = (
        GPU.objects_unscoped
        .filter(organization=api_key.organization)
        .select_related("node")
        .order_by("id")
    )
    if node_id := request.GET.get("node_id"):
        qs = qs.filter(node_id=node_id)
    if cluster_id := request.GET.get("cluster_id"):
        qs = qs.filter(node__cluster_id=cluster_id)
    if status := request.GET.get("status"):
        qs = qs.filter(status=status)
    return _paginated_response(qs, request, _ser_gpu)


@require_GET
def gpu_detail(request, gpu_uuid):
    """GET /api/v1/gpus/<uuid>/"""
    api_key, err = _require_scope(request, "read")
    if err:
        return err
    try:
        g = (
            GPU.objects_unscoped
            .select_related("node")
            .get(organization=api_key.organization, uuid=gpu_uuid)
        )
    except GPU.DoesNotExist:
        return JsonResponse({"error": "GPU not found"}, status=404)
    payload = _ser_gpu(g)
    payload["node"] = _ser_node(g.node) if g.node else None
    return JsonResponse(payload)


@require_GET
def list_inference_endpoints(request):
    """GET /api/v1/inference/endpoints/?status=&engine=&limit=&cursor="""
    api_key, err = _require_scope(request, "read")
    if err:
        return err
    qs = InferenceEndpoint.objects_unscoped.filter(organization=api_key.organization).order_by("id")
    if status := request.GET.get("status"):
        qs = qs.filter(status=status)
    if engine := request.GET.get("engine"):
        qs = qs.filter(engine=engine)
    return _paginated_response(qs, request, _ser_endpoint)


@require_GET
def costs_summary(request):
    """GET /api/v1/costs/summary/?hours=24"""
    api_key, err = _require_scope(request, "read")
    if err:
        return err
    try:
        hours = int(request.GET.get("hours", 24))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid 'hours'"}, status=400)
    if not (1 <= hours <= 720):
        return JsonResponse({"error": "'hours' must be between 1 and 720"}, status=400)

    summary = get_cost_summary(api_key.organization, period_hours=hours)
    summary["fleet_cost_per_hour"] = get_fleet_cost_rate(api_key.organization)
    summary["period_hours"] = hours
    return JsonResponse(summary)


@require_GET
def list_alert_rules(request):
    """GET /api/v1/alerts/rules/?enabled=true&limit=&cursor="""
    api_key, err = _require_scope(request, "read")
    if err:
        return err
    qs = AlertRule.objects.filter(organization=api_key.organization).order_by("id")
    enabled = request.GET.get("enabled")
    if enabled is not None:
        qs = qs.filter(is_enabled=(enabled.lower() == "true"))
    return _paginated_response(qs, request, _ser_alert_rule)


@require_GET
def list_alert_events(request):
    """GET /api/v1/alerts/events/?since=ISO&status=active|resolved&limit=&cursor="""
    api_key, err = _require_scope(request, "read")
    if err:
        return err
    qs = (
        AlertEvent.objects
        .filter(rule__organization=api_key.organization)
        .order_by("-id")
    )
    if since := request.GET.get("since"):
        parsed = parse_datetime(since)
        if parsed is None:
            return JsonResponse({"error": "Invalid 'since' (ISO datetime expected)"}, status=400)
        qs = qs.filter(triggered_at__gte=parsed)
    status = request.GET.get("status")
    if status == "active":
        qs = qs.filter(resolved_at__isnull=True)
    elif status == "resolved":
        qs = qs.filter(resolved_at__isnull=False)
    return _paginated_response(qs, request, _ser_alert_event)


# ── Inference ingest endpoint ─────────────────────────────────────────────────

@csrf_exempt
@require_POST
def ingest_inference(request):
    """
    POST /api/v1/ingest/inference/

    Accept a JSON payload from the inference scraper and persist metrics.

    Required header:
        X-API-Key: <key with 'ingest' scope>

    Request body (JSON):
        {
            "endpoint_name": "llama-70b",
            "model_name":    "meta-llama/Llama-3.1-70B",
            "engine":        "vllm",
            "metrics": { ... }
        }

    Response (200):
        { "status": "ok", "ingested": 1 }
    """
    # ── Auth ──────────────────────────────────────────────────────────────────
    api_key, err = authenticate_api_key(request)
    if err:
        return JsonResponse({"error": err}, status=401)

    if "ingest" not in (api_key.scopes or []):
        return JsonResponse({"error": "API key lacks 'ingest' scope"}, status=403)

    # ── Parse body ────────────────────────────────────────────────────────────
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, ValueError) as exc:
        return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)

    # ── Validate required fields ──────────────────────────────────────────────
    if not isinstance(payload.get("endpoint_name"), str) or not payload["endpoint_name"]:
        return JsonResponse({"error": "Missing or invalid 'endpoint_name'"}, status=400)
    if "metrics" in payload and not isinstance(payload["metrics"], dict):
        return JsonResponse({"error": "'metrics' must be a dict"}, status=400)

    # ── Ingest ────────────────────────────────────────────────────────────────
    try:
        count = ingest_inference_metrics(api_key.organization, payload)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("Inference metric ingestion failed: %s", exc)
        return JsonResponse({"error": "Ingestion failed"}, status=500)

    return JsonResponse({"status": "ok", "ingested": count})
