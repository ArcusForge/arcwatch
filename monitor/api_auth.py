"""
monitor/api_auth.py

Lightweight API key authentication for Django function-based views.
Does not depend on DRF — works with plain HttpRequest objects.
"""
import logging

from monitor.models import APIKey

logger = logging.getLogger(__name__)


def authenticate_api_key(request):
    """
    Validate the X-API-Key header on *request*.

    Returns:
        (APIKey instance, None)    — on success
        (None, error message str)  — on failure
    """
    raw_key = request.headers.get("X-Api-Key")
    if not raw_key:
        logger.warning("API auth failed: missing X-API-Key header from %s", request.META.get('REMOTE_ADDR'))
        return None, "Missing X-API-Key header"

    api_key = APIKey.authenticate(raw_key)
    if api_key is None:
        prefix = raw_key[:8] if len(raw_key) >= 8 else '***'
        logger.warning("API auth failed: invalid key from %s (prefix: %s)", request.META.get('REMOTE_ADDR'), prefix)
        return None, "Invalid or expired API key"

    return api_key, None
