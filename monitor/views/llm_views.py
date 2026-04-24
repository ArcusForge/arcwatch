"""monitor/views/llm_views.py — LLM API usage dashboard."""
import calendar
import datetime
import json

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from monitor.models import LLMUsageRecord, LLMProvider, ClaudeCodeUsageRecord


def _get_org(request):
    """Return the authenticated user's organization, or None."""
    return getattr(getattr(request.user, 'profile', None), 'organization', None)


@login_required
def llm_dashboard(request):
    org = _get_org(request)
    today = datetime.date.today()
    year, month = today.year, today.month
    days_in_month = calendar.monthrange(year, month)[1]
    days_elapsed = max(today.day, 1)

    records = list(
        LLMUsageRecord.objects.filter(
            organization=org,
            date__year=year,
            date__month=month,
        )
    ) if org else []

    # ── Totals ────────────────────────────────────────────────────────────────
    total_cost = sum(float(r.cost_usd) for r in records)
    total_requests = sum(r.request_count for r in records)

    projection = round(total_cost / days_elapsed * days_in_month, 4) if total_cost else 0

    # Cache hit rate (Anthropic only)
    anthr_records = [r for r in records if r.provider == "anthropic"]
    cache_hit_rate = None
    if anthr_records:
        anthr_input = sum(r.input_tokens for r in anthr_records)
        anthr_cache_read = sum(r.cache_read_tokens for r in anthr_records)
        denominator = anthr_input + anthr_cache_read
        cache_hit_rate = round(anthr_cache_read / denominator * 100, 1) if denominator else 0.0

    # ── By provider ───────────────────────────────────────────────────────────
    provider_totals: dict = {}
    for r in records:
        if r.provider not in provider_totals:
            provider_totals[r.provider] = {"cost": 0.0, "requests": 0}
        provider_totals[r.provider]["cost"] += float(r.cost_usd)
        provider_totals[r.provider]["requests"] += r.request_count

    by_provider = [
        {
            "provider": p,
            "cost": round(d["cost"], 4),
            "requests": d["requests"],
            "pct": round(d["cost"] / total_cost * 100, 1) if total_cost else 0,
        }
        for p, d in sorted(provider_totals.items(), key=lambda x: -x[1]["cost"])
    ]

    # ── By model ──────────────────────────────────────────────────────────────
    model_totals: dict = {}
    for r in records:
        key = (r.provider, r.model)
        if key not in model_totals:
            model_totals[key] = {
                "provider": r.provider, "model": r.model,
                "requests": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_creation_tokens": 0, "cache_read_tokens": 0, "cost": 0.0,
            }
        d = model_totals[key]
        d["requests"] += r.request_count
        d["input_tokens"] += r.input_tokens
        d["output_tokens"] += r.output_tokens
        d["cache_creation_tokens"] += r.cache_creation_tokens
        d["cache_read_tokens"] += r.cache_read_tokens
        d["cost"] += float(r.cost_usd)

    by_model = sorted(model_totals.values(), key=lambda x: -x["cost"])
    for m in by_model:
        m["cost"] = round(m["cost"], 4)
        denominator = m["input_tokens"] + m["cache_read_tokens"]
        m["cache_hit_rate"] = (
            round(m["cache_read_tokens"] / denominator * 100, 1) if denominator else None
        )

    has_anthropic = any(m["provider"] == "anthropic" for m in by_model)

    # ── Daily chart (last 30 days) ────────────────────────────────────────────
    since_30 = today - datetime.timedelta(days=29)
    daily_records = list(
        LLMUsageRecord.objects.filter(
            organization=org,
            date__gte=since_30,
        )
    ) if org else []

    day_map: dict = {}
    for r in daily_records:
        ds = r.date.isoformat()
        if ds not in day_map:
            day_map[ds] = {"anthropic": 0.0, "openai": 0.0}
        day_map[ds][r.provider] = day_map[ds].get(r.provider, 0.0) + float(r.cost_usd)

    chart_labels = []
    chart_anthropic = []
    chart_openai = []
    d = since_30
    while d <= today:
        ds = d.isoformat()
        chart_labels.append(d.strftime("%b %d"))
        vals = day_map.get(ds, {})
        chart_anthropic.append(round(vals.get("anthropic", 0.0), 4))
        chart_openai.append(round(vals.get("openai", 0.0), 4))
        d += datetime.timedelta(days=1)

    return render(request, "monitor/llm_dashboard.html", {
        "total_cost": round(total_cost, 4),
        "total_requests": total_requests,
        "projection": projection,
        "cache_hit_rate": cache_hit_rate,
        "by_provider": by_provider,
        "by_model": by_model,
        "has_anthropic": has_anthropic,
        "chart_labels": json.dumps(chart_labels),
        "chart_anthropic": json.dumps(chart_anthropic),
        "chart_openai": json.dumps(chart_openai),
        "month_name": today.strftime("%B %Y"),
    })


@login_required
def llm_setup(request):
    org = _get_org(request)
    has_providers = org is not None and LLMProvider.objects.filter(organization=org).exists()
    return render(request, "monitor/llm_setup.html", {
        "has_providers": has_providers,
    })


@login_required
def claude_code_dashboard(request):
    org = _get_org(request)
    today = datetime.date.today()
    year, month = today.year, today.month
    days_in_month = calendar.monthrange(year, month)[1]
    days_elapsed = max(today.day, 1)

    records = list(
        ClaudeCodeUsageRecord.objects.filter(
            organization=org,
            date__year=year,
            date__month=month,
        )
    ) if org else []

    # ── KPI totals ────────────────────────────────────────────────────────────
    total_sessions = sum(r.sessions for r in records)
    total_lines_added = sum(r.lines_added for r in records)
    total_commits = sum(r.commits for r in records)
    total_prs = sum(r.prs for r in records)
    total_cost = sum(float(r.cost_usd) for r in records)

    projection = round(total_cost / days_elapsed * days_in_month, 4) if total_cost else 0

    # ── Per-user breakdown ────────────────────────────────────────────────────
    user_totals: dict = {}
    for r in records:
        if r.user_email not in user_totals:
            user_totals[r.user_email] = {
                "user_email": r.user_email,
                "customer_type": r.customer_type,
                "sessions": 0, "lines_added": 0, "lines_removed": 0,
                "commits": 0, "prs": 0,
                "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                "cost": 0.0,
            }
        d = user_totals[r.user_email]
        d["sessions"] += r.sessions
        d["lines_added"] += r.lines_added
        d["lines_removed"] += r.lines_removed
        d["commits"] += r.commits
        d["prs"] += r.prs
        d["input_tokens"] += r.input_tokens
        d["output_tokens"] += r.output_tokens
        d["cache_read_tokens"] += r.cache_read_tokens
        d["cost"] += float(r.cost_usd)

    by_user = sorted(user_totals.values(), key=lambda x: -x["sessions"])
    for u in by_user:
        u["cost"] = round(u["cost"], 4)

    # ── Daily chart — sessions last 30 days ───────────────────────────────────
    since_30 = today - datetime.timedelta(days=29)
    daily_records = list(
        ClaudeCodeUsageRecord.objects.filter(
            organization=org,
            date__gte=since_30,
        )
    ) if org else []

    day_sessions: dict = {}
    day_cost: dict = {}
    for r in daily_records:
        ds = r.date.isoformat()
        day_sessions[ds] = day_sessions.get(ds, 0) + r.sessions
        day_cost[ds] = day_cost.get(ds, 0.0) + float(r.cost_usd)

    chart_labels = []
    chart_sessions = []
    chart_cost = []
    d = since_30
    while d <= today:
        ds = d.isoformat()
        chart_labels.append(d.strftime("%b %d"))
        chart_sessions.append(day_sessions.get(ds, 0))
        chart_cost.append(round(day_cost.get(ds, 0.0), 4))
        d += datetime.timedelta(days=1)

    # Check if we have an active Anthropic provider (for "Sync Now" option)
    has_anthropic_provider = (
        org is not None and
        LLMProvider.objects.filter(organization=org, provider="anthropic", is_active=True).exists()
    )

    return render(request, "monitor/claude_code_dashboard.html", {
        "total_sessions": total_sessions,
        "total_lines_added": total_lines_added,
        "total_commits": total_commits,
        "total_prs": total_prs,
        "total_cost": round(total_cost, 4),
        "projection": projection,
        "by_user": by_user,
        "chart_labels": json.dumps(chart_labels),
        "chart_sessions": json.dumps(chart_sessions),
        "chart_cost": json.dumps(chart_cost),
        "month_name": today.strftime("%B %Y"),
        "has_anthropic_provider": has_anthropic_provider,
        "has_data": bool(records),
    })
