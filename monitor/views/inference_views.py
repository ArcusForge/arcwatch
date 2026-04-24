"""
monitor/views/inference_views.py

Inference Dashboard view — lists all InferenceEndpoints with live metrics.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from monitor.models import InferenceEndpoint


@login_required
def inference_dashboard(request):
    """
    Render the inference endpoints dashboard.

    Lists all InferenceEndpoints (unscoped so demo data is always visible)
    with their current metrics, and computes fleet-level KPIs.
    """
    org = getattr(getattr(request.user, 'profile', None), 'organization', None)

    qs = InferenceEndpoint.objects_unscoped.select_related('organization', 'team').order_by('name')
    if org is not None:
        qs = qs.filter(organization=org)
    endpoints = list(qs)

    total_endpoints = len(endpoints)

    if total_endpoints == 0:
        return render(request, "monitor/inference_dashboard.html", {
            "endpoints": [],
            "total_endpoints": 0,
            "avg_latency": None,
            "total_tokens_per_sec": None,
            "avg_kv_cache_pct": None,
            "serving_count": 0,
            "error_count": 0,
        })

    # ── Aggregate KPIs ────────────────────────────────────────────────────────
    serving_count = sum(1 for e in endpoints if e.status == 'serving')
    error_count = sum(1 for e in endpoints if e.status == 'error')

    latency_values = [
        e.current_avg_latency_ms
        for e in endpoints
        if e.current_avg_latency_ms is not None
    ]
    avg_latency = (
        round(sum(latency_values) / len(latency_values), 1)
        if latency_values else None
    )

    tps_values = [
        e.current_tokens_per_sec
        for e in endpoints
        if e.current_tokens_per_sec is not None
    ]
    total_tokens_per_sec = round(sum(tps_values), 1) if tps_values else None

    kv_values = [
        e.current_kv_cache_usage_pct
        for e in endpoints
        if e.current_kv_cache_usage_pct is not None
    ]
    avg_kv_cache_pct = (
        round(sum(kv_values) / len(kv_values), 1)
        if kv_values else None
    )

    context = {
        "endpoints": endpoints,
        "total_endpoints": total_endpoints,
        "avg_latency": avg_latency,
        "total_tokens_per_sec": total_tokens_per_sec,
        "avg_kv_cache_pct": avg_kv_cache_pct,
        "serving_count": serving_count,
        "error_count": error_count,
    }
    return render(request, "monitor/inference_dashboard.html", context)
