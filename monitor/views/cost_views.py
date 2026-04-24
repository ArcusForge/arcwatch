"""
monitor/views/cost_views.py

Cost Attribution Dashboard view.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from monitor.models import GPU, GPUNode, GPUPricing
from monitor.services.cost_engine import get_cost_summary, get_fleet_cost_rate


@login_required
def cost_dashboard(request):
    """
    Render the cost attribution dashboard.

    Uses the first available organization that has GPUs, falling back
    to the first org overall if none have GPUs yet.
    """
    # Use the authenticated user's organization
    org = getattr(getattr(request.user, 'profile', None), 'organization', None)

    if org is None:
        return render(request, "monitor/cost_attribution.html", {
            "total_cost": 0.0,
            "total_waste": 0.0,
            "waste_pct": 0.0,
            "cost_per_hour": 0.0,
            "by_model": [],
            "by_node": [],
            "pricing_entries": [],
        })

    summary = get_cost_summary(org, period_hours=24)
    cost_per_hour = get_fleet_cost_rate(org)

    total_cost = summary["total_cost"]
    total_waste = summary["total_waste"]
    waste_pct = (
        round(total_waste / total_cost * 100, 1) if total_cost > 0 else 0.0
    )

    # Enrich by_model with efficiency %
    for entry in summary["by_model"]:
        cost = entry["total_cost"]
        waste = entry["total_waste"]
        entry["efficiency_pct"] = (
            round((cost - waste) / cost * 100, 1) if cost > 0 else 100.0
        )

    # Enrich by_node with additional node info
    node_info = {
        n.hostname: {
            "gpu_count": n.gpu_count,
            "gpu_type": n.gpu_type,
            "hourly_cost": float(n.hourly_cost) if n.hourly_cost else None,
        }
        for n in GPUNode.objects_unscoped.filter(organization=org)
    }
    for entry in summary["by_node"]:
        info = node_info.get(entry["node_name"], {})
        entry["gpu_count"] = info.get("gpu_count", 0)
        entry["gpu_type"] = info.get("gpu_type", "")
        entry["hourly_rate"] = info.get("hourly_cost")

    pricing_entries = list(GPUPricing.objects.all())

    context = {
        "total_cost": total_cost,
        "total_waste": total_waste,
        "waste_pct": waste_pct,
        "cost_per_hour": cost_per_hour,
        "by_model": summary["by_model"],
        "by_node": summary["by_node"],
        "pricing_entries": pricing_entries,
    }
    return render(request, "monitor/cost_attribution.html", context)
