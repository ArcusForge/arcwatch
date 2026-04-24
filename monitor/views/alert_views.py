"""
monitor/views/alert_views.py

Alerts Dashboard view — lists AlertRules and recent AlertEvents.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from monitor.models import AlertEvent, AlertRule


@login_required
def alerts_dashboard(request):
    """
    Render the alerts dashboard.

    Lists all AlertRules (for the first available org) and recent
    AlertEvents.
    """
    org = getattr(getattr(request.user, 'profile', None), 'organization', None)

    if org is None:
        return render(request, "monitor/alerts_dashboard.html", {
            "rules": [],
            "events": [],
            "active_count": 0,
            "total_rules": 0,
            "enabled_rules": 0,
        })

    rules = list(
        AlertRule.objects
        .filter(organization=org)
        .order_by('-is_enabled', 'name')
    )

    events = list(
        AlertEvent.objects
        .filter(rule__organization=org)
        .select_related('rule')
        .order_by('-triggered_at')[:50]
    )

    active_count = sum(1 for e in events if e.resolved_at is None)
    total_rules = len(rules)
    enabled_rules = sum(1 for r in rules if r.is_enabled)

    context = {
        "rules": rules,
        "events": events,
        "active_count": active_count,
        "total_rules": total_rules,
        "enabled_rules": enabled_rules,
    }
    return render(request, "monitor/alerts_dashboard.html", context)
