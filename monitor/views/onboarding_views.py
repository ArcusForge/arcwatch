"""
monitor/views/onboarding_views.py -- Views for users without an assigned organization.

These views are reachable without an organization context; they are in the
TenantMiddleware allowlist so tenant resolution is skipped.
"""
import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from monitor.models import Organization
from monitor.services.demo_seeder import seed_demo_fleet

logger = logging.getLogger(__name__)


# Default seed depth for the Demo Fleet button. 7 days = enough to populate
# the cost trend chart (which shows 7d) without making the request slow.
DEMO_FLEET_HOURS = 168


def no_organization(request):
    """Render a friendly page for authenticated users whose profile has no org."""
    return render(request, "monitor/no_organization.html", {
        "user": request.user if request.user.is_authenticated else None,
    })


def _resolve_or_create_personal_org(user):
    """Return user's org, creating a personal one if missing. Sets profile.role='owner'."""
    profile = getattr(user, "profile", None)
    if profile is not None and profile.organization is not None:
        return profile.organization

    base_slug = slugify(user.username) or f"user-{user.pk}"
    slug = base_slug
    n = 1
    while Organization.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base_slug}-{n}"

    org = Organization.objects.create(
        name=f"{user.username}'s Workspace",
        slug=slug,
        owner=user,
        plan="free",
    )
    if profile is not None:
        profile.organization = org
        profile.role = "owner"
        profile.save(update_fields=["organization", "role"])
    return org


@login_required
@require_POST
def enable_demo_fleet(request):
    """
    POST /onboarding/enable-demo/

    Provision a Demo Fleet cluster + 7 days of synthetic metrics into the
    user's org (creating a personal org if they don't have one). Idempotent.
    """
    org = _resolve_or_create_personal_org(request.user)

    try:
        result = seed_demo_fleet(
            org=org,
            user=request.user,
            nodes=4,
            gpus_per_node=4,
            hours=DEMO_FLEET_HOURS,
        )
        logger.info(
            "Demo fleet seeded for org=%s user=%s gpus=%d metric_rows=%d",
            org.slug, request.user.username,
            result["gpus"], result["gpu_metrics_rows"],
        )
    except Exception as exc:
        logger.exception("Demo fleet seed failed for user=%s: %s", request.user.username, exc)
        return render(request, "monitor/no_organization.html", {
            "user": request.user,
            "demo_error": str(exc),
        }, status=500)

    return HttpResponseRedirect(reverse("monitor:gpu_fleet_dashboard"))
