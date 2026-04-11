"""
monitor/views/onboarding_views.py -- Views for users without an assigned organization.

These views are reachable without an organization context; they are in the
TenantMiddleware allowlist so tenant resolution is skipped.
"""
from django.shortcuts import render


def no_organization(request):
    """Render a friendly page for authenticated users whose profile has no org."""
    return render(request, "monitor/no_organization.html", {
        "user": request.user if request.user.is_authenticated else None,
    })
