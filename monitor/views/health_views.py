"""
monitor/views/health_views.py -- Health check endpoints for load balancers and monitoring.
"""
import logging

from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)


def healthz(request):
    """Liveness probe -- returns 200 if the app is running."""
    return JsonResponse({"status": "ok"})


def readyz(request):
    """Readiness probe -- returns 200 if database is reachable."""
    checks = {}

    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        logger.error("Readiness check failed -- database: %s", exc)
        return JsonResponse({"status": "unhealthy", "checks": checks}, status=503)

    try:
        from django.conf import settings
        redis_url = getattr(settings, 'REDIS_URL', None) or getattr(settings, 'CELERY_BROKER_URL', None)
        if redis_url:
            import redis
            r = redis.from_url(redis_url)
            r.ping()
            checks["redis"] = "ok"
        else:
            checks["redis"] = "not configured"
    except ImportError:
        checks["redis"] = "redis package not installed"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"
        logger.error("Readiness check failed -- redis: %s", exc)
        return JsonResponse({"status": "unhealthy", "checks": checks}, status=503)

    return JsonResponse({"status": "ok", "checks": checks})
