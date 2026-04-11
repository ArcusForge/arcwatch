"""
monitor/decorators.py -- Shared auth/RBAC decorators for settings views.
"""
from functools import wraps

from django.http import HttpResponseForbidden


def require_admin(view_func):
    """
    Require the logged-in user to have role 'admin' or 'owner'.
    Must be used AFTER @login_required (assumes request.user is authenticated).
    Returns HTTP 403 for viewer and operator roles.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponseForbidden("Admin access required.")
        try:
            role = request.user.profile.role
        except AttributeError:
            return HttpResponseForbidden("Admin access required.")
        if role not in ('admin', 'owner'):
            return HttpResponseForbidden("Admin access required.")
        return view_func(request, *args, **kwargs)
    return _wrapped


def require_operator(view_func):
    """
    Require the logged-in user to have role 'operator', 'admin', or 'owner'.
    Must be used AFTER @login_required (assumes request.user is authenticated
    in the happy path; returns 403 for unauthenticated requests as a safety net).
    Returns HTTP 403 for viewer role and unauthenticated requests.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponseForbidden("Operator access required.")
        try:
            role = request.user.profile.role
        except AttributeError:
            return HttpResponseForbidden("Operator access required.")
        if role not in ('operator', 'admin', 'owner'):
            return HttpResponseForbidden("Operator access required.")
        return view_func(request, *args, **kwargs)
    return _wrapped


def is_htmx(request):
    """Return True if the request was made by HTMX (HX-Request header present)."""
    return request.headers.get('HX-Request') == 'true'
