"""
monitor/views/chart_views.py

Internal Chart.js-shaped JSON endpoints for HTMX-loaded dashboard charts.

Auth: session-based (login_required). NOT exposed via X-API-Key.
Tenant: scoped via request.user.profile.organization.
Response shape: {"labels": [...], "datasets": [{"label": "...", "data": [...], ...}]}

These endpoints serve the four primary dashboards. They are SQLite-compatible
for the test suite (TimescaleDB time_bucket() falls back to hour-truncation).
"""
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.utils import timezone

from monitor.models import GPU, InferenceEndpoint, AlertEvent


def _get_org(request):
    return getattr(getattr(request.user, "profile", None), "organization", None)


def _is_sqlite() -> bool:
    return "sqlite" in connection.settings_dict.get("ENGINE", "")


def _empty_chart(label: str = "") -> dict:
    return {"labels": [], "datasets": [{"label": label, "data": []}]}


# ── Chart 1: GPU utilization timeseries (last 24h, 15-min buckets) ────────────

@login_required
def chart_gpu_util_timeseries(request):
    org = _get_org(request)
    if org is None:
        return JsonResponse(_empty_chart("Avg Utilization %"))

    gpu_uuids = list(
        GPU.objects_unscoped.filter(organization=org).values_list("uuid", flat=True)
    )
    if not gpu_uuids:
        return JsonResponse(_empty_chart("Avg Utilization %"))

    placeholders = ",".join(["%s"] * len(gpu_uuids))

    if _is_sqlite():
        # 15-minute buckets via integer arithmetic on epoch seconds
        sql = f"""
            SELECT
                strftime('%Y-%m-%dT%H:%M:00',
                    datetime((CAST(strftime('%s', time) AS INTEGER) / 900) * 900, 'unixepoch')) AS bucket,
                AVG(utilization) AS avg_util
            FROM gpu_metrics
            WHERE time >= datetime('now', '-24 hours')
              AND gpu_uuid IN ({placeholders})
            GROUP BY bucket
            ORDER BY bucket
        """
        params = gpu_uuids
    else:
        sql = f"""
            SELECT
                time_bucket('15 minutes', time) AS bucket,
                AVG(utilization) AS avg_util
            FROM gpu_metrics
            WHERE time >= NOW() - INTERVAL '24 hours'
              AND gpu_uuid IN ({placeholders})
            GROUP BY bucket
            ORDER BY bucket
        """
        params = gpu_uuids

    with connection.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    labels = [str(r[0]) for r in rows]
    data = [round(float(r[1]), 1) if r[1] is not None else None for r in rows]

    return JsonResponse({
        "labels": labels,
        "datasets": [{
            "label": "Avg Utilization %",
            "data": data,
            "borderColor": "#76B900",
            "backgroundColor": "rgba(118, 185, 0, 0.1)",
            "fill": True,
            "tension": 0.3,
        }],
    })


# ── Chart 2: Cost trend (last 7d, hourly) ─────────────────────────────────────

@login_required
def chart_cost_trend(request):
    org = _get_org(request)
    if org is None:
        return JsonResponse(_empty_chart("Cost ($)"))

    gpu_uuids = list(
        GPU.objects_unscoped.filter(organization=org).values_list("uuid", flat=True)
    )
    if not gpu_uuids:
        return JsonResponse(_empty_chart("Cost ($)"))

    placeholders = ",".join(["%s"] * len(gpu_uuids))

    if _is_sqlite():
        sql = f"""
            SELECT
                strftime('%Y-%m-%dT%H:00:00', time) AS bucket,
                COALESCE(SUM(cost_this_period), 0) AS total_cost,
                COALESCE(SUM(waste_this_period), 0) AS total_waste
            FROM cost_snapshots
            WHERE time >= datetime('now', '-7 days')
              AND gpu_uuid IN ({placeholders})
            GROUP BY bucket
            ORDER BY bucket
        """
        params = gpu_uuids
    else:
        sql = f"""
            SELECT
                time_bucket('1 hour', time) AS bucket,
                COALESCE(SUM(cost_this_period), 0) AS total_cost,
                COALESCE(SUM(waste_this_period), 0) AS total_waste
            FROM cost_snapshots
            WHERE time >= NOW() - INTERVAL '7 days'
              AND gpu_uuid IN ({placeholders})
            GROUP BY bucket
            ORDER BY bucket
        """
        params = gpu_uuids

    with connection.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return JsonResponse({
        "labels": [str(r[0]) for r in rows],
        "datasets": [
            {
                "label": "Cost ($)",
                "data": [round(float(r[1]), 4) for r in rows],
                "borderColor": "#76B900",
                "backgroundColor": "rgba(118, 185, 0, 0.15)",
                "fill": True,
                "tension": 0.3,
            },
            {
                "label": "Waste ($)",
                "data": [round(float(r[2]), 4) for r in rows],
                "borderColor": "#f87171",
                "backgroundColor": "rgba(248, 113, 113, 0.1)",
                "fill": True,
                "tension": 0.3,
            },
        ],
    })


# ── Chart 3: Alert timeline (last 24h, hourly, stacked by severity) ───────────

@login_required
def chart_alert_timeline(request):
    org = _get_org(request)
    if org is None:
        return JsonResponse({"labels": [], "datasets": []})

    cutoff = timezone.now() - timedelta(hours=24)
    events = (
        AlertEvent.objects
        .filter(rule__organization=org, triggered_at__gte=cutoff)
        .values_list("triggered_at", "severity")
    )

    # Bucket into hourly slots, key = (hour_iso, severity)
    counts: dict = {}
    for ts, severity in events:
        bucket = ts.replace(minute=0, second=0, microsecond=0).isoformat()
        counts.setdefault(bucket, {"info": 0, "warning": 0, "critical": 0})
        if severity in counts[bucket]:
            counts[bucket][severity] += 1

    # Generate complete hour range so empty buckets render
    labels = []
    now_floor = timezone.now().replace(minute=0, second=0, microsecond=0)
    for h in range(24, -1, -1):
        labels.append((now_floor - timedelta(hours=h)).isoformat())

    def series(sev: str) -> list:
        return [counts.get(label, {}).get(sev, 0) for label in labels]

    return JsonResponse({
        "labels": labels,
        "datasets": [
            {"label": "Critical", "data": series("critical"),
             "backgroundColor": "#f87171", "stack": "alerts"},
            {"label": "Warning", "data": series("warning"),
             "backgroundColor": "#fbbf24", "stack": "alerts"},
            {"label": "Info", "data": series("info"),
             "backgroundColor": "#60a5fa", "stack": "alerts"},
        ],
    })


# ── Chart 4: Inference latency p50/p95/p99 by endpoint ────────────────────────

@login_required
def chart_inference_latency(request):
    org = _get_org(request)
    if org is None:
        return JsonResponse({"labels": [], "datasets": []})

    endpoints = list(
        InferenceEndpoint.objects_unscoped
        .filter(organization=org, is_active=True)
        .values_list("id", "name")
    )
    if not endpoints:
        return JsonResponse({"labels": [], "datasets": []})

    # endpoint_id in inference_metrics is hashed int — match by hash
    ep_id_to_name = {abs(hash(str(eid))) % (2 ** 31): name for eid, name in endpoints}
    ep_int_ids = list(ep_id_to_name.keys())
    placeholders = ",".join(["%s"] * len(ep_int_ids))

    if _is_sqlite():
        sql = f"""
            SELECT endpoint_id,
                   AVG(latency_p50), AVG(latency_p95), AVG(latency_p99)
            FROM inference_metrics
            WHERE time >= datetime('now', '-24 hours')
              AND endpoint_id IN ({placeholders})
            GROUP BY endpoint_id
        """
    else:
        sql = f"""
            SELECT endpoint_id,
                   AVG(latency_p50), AVG(latency_p95), AVG(latency_p99)
            FROM inference_metrics
            WHERE time >= NOW() - INTERVAL '24 hours'
              AND endpoint_id IN ({placeholders})
            GROUP BY endpoint_id
        """

    with connection.cursor() as cur:
        cur.execute(sql, ep_int_ids)
        rows = cur.fetchall()

    # Preserve endpoint order; missing endpoints get None
    by_eid = {r[0]: (r[1], r[2], r[3]) for r in rows}
    labels = [name for _, name in endpoints]
    p50, p95, p99 = [], [], []
    for eid, _ in endpoints:
        eid_int = abs(hash(str(eid))) % (2 ** 31)
        agg = by_eid.get(eid_int)
        if agg:
            p50.append(round(float(agg[0] or 0), 1))
            p95.append(round(float(agg[1] or 0), 1))
            p99.append(round(float(agg[2] or 0), 1))
        else:
            p50.append(0)
            p95.append(0)
            p99.append(0)

    return JsonResponse({
        "labels": labels,
        "datasets": [
            {"label": "p50 (ms)", "data": p50, "backgroundColor": "#4ade80"},
            {"label": "p95 (ms)", "data": p95, "backgroundColor": "#fbbf24"},
            {"label": "p99 (ms)", "data": p99, "backgroundColor": "#f87171"},
        ],
    })
