"""
monitor/rest_api.py

Plain Django function-based REST API views.
No DRF required -- responses are plain JSON.
"""
import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from monitor.api_auth import authenticate_api_key
from monitor.models import GPUCluster
from monitor.services.metric_ingestion import ingest_gpu_metrics
from monitor.services.inference_ingestion import ingest_inference_metrics

logger = logging.getLogger(__name__)


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
