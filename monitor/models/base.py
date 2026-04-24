"""
monitor/models/base.py -- Tenant-aware manager and contextvars-based org scoping helpers.
"""
import contextvars

from django.db import models


# ── Context-variable storage (async-safe, unlike threading.local) ────────────

_current_org: contextvars.ContextVar = contextvars.ContextVar('current_org', default=None)


# ── Tenant-Aware Manager ──────────────────────────────────────────────────────

class TenantManager(models.Manager):
    """
    Drop-in manager that auto-filters by the current org when
    set_current_org(org) has been called in the request context.

    Activated by TenantMiddleware on every authenticated request.

    Views that need cross-org access (admin ops, Celery tasks) should use
    Model.objects_unscoped instead.
    """

    def get_queryset(self):
        qs = super().get_queryset()
        org = _current_org.get()
        if org is not None and hasattr(self.model, 'organization'):
            qs = qs.filter(organization=org)
        return qs


def set_current_org(org):
    """Call at the start of a request to scope all subsequent ORM queries."""
    _current_org.set(org)


def clear_current_org():
    """Call at the end of a request to clear tenant scoping."""
    _current_org.set(None)
