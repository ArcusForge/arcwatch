"""
monitor/views/dashboard_views.py

GPU Fleet Dashboard view — renders the real-time GPU fleet overview page.
"""
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Count, Sum
from django.shortcuts import redirect, render

from monitor.models import GPU, GPUNode


@login_required
def gpu_fleet_dashboard(request):
    """
    Render the GPU fleet dashboard.

    Queries all GPU records, computes aggregate KPIs, and passes a rich
    context to the gpu_fleet_dashboard template.
    """
    # ── Resolve the user's organization ─────────────────────────────────────
    org = getattr(getattr(request.user, 'profile', None), 'organization', None)

    # ── All GPUs for this org ────────────────────────────────────────────────
    qs = GPU.objects_unscoped.select_related("node").order_by("node__hostname", "gpu_index")
    if org is not None:
        qs = qs.filter(organization=org)
    gpus = list(qs)

    total_gpus = len(gpus)

    if total_gpus == 0:
        return render(request, "monitor/gpu_fleet_dashboard.html", {
            "gpus": [],
            "total_gpus": 0,
            "active_count": 0,
            "avg_utilization": None,
            "avg_temperature": None,
            "vram_used_gb": 0,
            "vram_total_gb": 0,
            "vram_pct": 0,
            "fleet_cost_hr": None,
            "util_high_count": 0,
            "util_mid_count": 0,
            "util_low_count": 0,
        })

    # ── Aggregate KPIs ────────────────────────────────────────────────────────
    active_count = sum(1 for g in gpus if g.status in ("healthy", "active"))

    util_values = [g.current_utilization for g in gpus if g.current_utilization is not None]
    avg_utilization = round(sum(util_values) / len(util_values), 1) if util_values else None

    temp_values = [g.current_temperature_c for g in gpus if g.current_temperature_c is not None]
    avg_temperature = round(sum(temp_values) / len(temp_values), 1) if temp_values else None

    vram_used_mb = sum(g.current_memory_used_mb or 0 for g in gpus)
    vram_total_mb = sum(g.current_memory_total_mb or 0 for g in gpus)
    vram_used_gb = round(vram_used_mb / 1024, 1)
    vram_total_gb = round(vram_total_mb / 1024, 1)
    vram_pct = round(vram_used_mb / vram_total_mb * 100, 1) if vram_total_mb > 0 else 0

    # ── Fleet hourly cost (sum of unique node hourly_cost values) ────────────
    seen_node_ids = set()
    fleet_cost_hr = 0.0
    for g in gpus:
        if g.node_id not in seen_node_ids and g.node.hourly_cost is not None:
            fleet_cost_hr += float(g.node.hourly_cost)
            seen_node_ids.add(g.node_id)
    fleet_cost_hr = round(fleet_cost_hr, 2) if fleet_cost_hr else None

    # ── Utilization band counts ───────────────────────────────────────────────
    util_high_count = sum(1 for g in gpus if g.current_utilization is not None and g.current_utilization > 60)
    util_mid_count  = sum(1 for g in gpus if g.current_utilization is not None and 30 <= g.current_utilization <= 60)
    util_low_count  = sum(1 for g in gpus if g.current_utilization is not None and g.current_utilization < 30)

    # ── Annotate each GPU with display helpers ────────────────────────────────
    annotated_gpus = []
    for gpu in gpus:
        util = gpu.current_utilization
        if util is not None:
            if util > 60:
                util_class = "hi"
            elif util >= 30:
                util_class = "mid"
            else:
                util_class = "ok"
        else:
            util_class = "ok"

        temp = gpu.current_temperature_c
        if temp is not None:
            if temp > 80:
                temp_class = "hi"
            elif temp > 65:
                temp_class = "mid"
            else:
                temp_class = "ok"
        else:
            temp_class = "ok"

        annotated_gpus.append({
            "obj": gpu,
            "util_class": util_class,
            "temp_class": temp_class,
            "util_pct": round(util, 1) if util is not None else 0,
            "mem_used_gb": round((gpu.current_memory_used_mb or 0) / 1024, 1),
            "mem_total_gb": round((gpu.current_memory_total_mb or 0) / 1024, 1),
        })

    context = {
        "gpus": annotated_gpus,
        "total_gpus": total_gpus,
        "active_count": active_count,
        "avg_utilization": avg_utilization,
        "avg_temperature": avg_temperature,
        "vram_used_gb": vram_used_gb,
        "vram_total_gb": vram_total_gb,
        "vram_pct": vram_pct,
        "fleet_cost_hr": fleet_cost_hr,
        "util_high_count": util_high_count,
        "util_mid_count": util_mid_count,
        "util_low_count": util_low_count,
    }
    return render(request, "monitor/gpu_fleet_dashboard.html", context)


def landing(request):
    """Public landing page. Authenticated users are sent straight to the dashboard."""
    if request.user.is_authenticated:
        return redirect('monitor:gpu_fleet_dashboard')
    return render(request, "monitor/landing.html")
