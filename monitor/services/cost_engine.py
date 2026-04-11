"""
monitor/services/cost_engine.py

Cost attribution engine.

Functions:
  compute_cost_snapshot()   -- Celery periodic task (every minute).
  get_cost_summary(org)     -- Query cost_snapshots; totals by model / node.
  get_fleet_cost_rate(org)  -- Current $/hr burn rate for an org.
"""
import hashlib
import logging
from decimal import Decimal

from celery import shared_task
from django.db import connection, transaction
from django.utils import timezone as django_tz

from monitor.models import GPU, GPUPricing, Organization

try:
    from psycopg2.extras import execute_values
except ImportError:  # pragma: no cover
    execute_values = None

logger = logging.getLogger(__name__)

# Interval in seconds this task fires at (60 s == 1 minute)
INTERVAL_SECONDS = 60


# ── Celery periodic task ──────────────────────────────────────────────────────

@shared_task(name='monitor.compute_cost_snapshot')
def compute_cost_snapshot():
    """
    Celery task -- runs every minute.

    For each GPU in the fleet:
      1. Match its model name against GPUPricing patterns.
      2. Compute cost_this_period = hourly_rate * (INTERVAL_SECONDS / 3600).
      3. Compute waste_this_period = cost_this_period * (1 - utilization/100).
      4. Write a row to cost_snapshots.

    Each org's snapshot is wrapped in its own transaction.atomic block so a
    failure in one org does not roll back snapshots already committed for
    other orgs. A per-org except block catches the rollback and continues to
    the next org instead of aborting the task.
    """
    # Pre-load all pricing entries once
    pricing_entries = list(GPUPricing.objects.all())

    now = django_tz.now()
    ts = now.isoformat()

    insert_sql = """
        INSERT INTO cost_snapshots (
            time, gpu_uuid, endpoint_id,
            model_name, hourly_rate,
            utilization, cost_this_period, waste_this_period
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """

    orgs = Organization.objects.all()
    total_rows = 0

    # On SQLite (tests), execute_values is not compatible with the cursor
    # type — fall back to executemany in that case.
    use_execute_values = (
        execute_values is not None and connection.vendor == "postgresql"
    )

    for org in orgs:
        try:
            with transaction.atomic(using='default'):
                gpus = GPU.objects_unscoped.filter(
                    organization=org, status__in=('healthy', 'active', 'degraded')
                ).select_related('node').iterator(chunk_size=500)

                rows = []
                for gpu in gpus:
                    model_str = gpu.current_model_name or gpu.node.gpu_type or ''
                    rate = _match_pricing(model_str, pricing_entries)

                    utilization = gpu.current_utilization or 0.0
                    cost = float(rate) * (INTERVAL_SECONDS / 3600.0) if rate else 0.0
                    waste = cost * (1.0 - utilization / 100.0)

                    # endpoint_id: use the FK int-id if available, else None
                    ep_id = None
                    if gpu.current_endpoint_id_id is not None:
                        ep_id = _ep_int_id(str(gpu.current_endpoint_id_id))

                    rows.append((
                        ts,
                        gpu.uuid,
                        ep_id,
                        gpu.current_model_name or None,
                        float(rate) if rate else None,
                        utilization,
                        round(cost, 8),
                        round(waste, 8),
                    ))

                if rows:
                    with connection.cursor() as cur:
                        if use_execute_values:
                            execute_values(cur, """
                                INSERT INTO cost_snapshots (
                                    time, gpu_uuid, endpoint_id,
                                    model_name, hourly_rate,
                                    utilization, cost_this_period, waste_this_period
                                ) VALUES %s
                            """, rows, page_size=1000)
                        else:
                            cur.executemany(insert_sql, rows)

                total_rows += len(rows)
        except Exception as exc:
            logger.error(
                "Cost snapshot rollback for org %s: %s",
                getattr(org, 'slug', org.pk), exc,
            )

    logger.info("Cost snapshot: wrote %d rows", total_rows)
    return total_rows


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_cost_summary(org, period_hours: int = 24) -> dict:
    """
    Return cost totals for *org* over the last *period_hours* hours.

    Returns a dict with keys:
      total_cost      -- float
      total_waste     -- float
      by_model        -- list of {model_name, total_cost, total_waste, gpu_hours}
      by_node         -- list of {node_name, total_cost, total_waste, utilization_avg}
    """
    # Get all GPU uuids for this org
    gpu_uuids = list(
        GPU.objects_unscoped
        .filter(organization=org)
        .values_list('uuid', flat=True)
    )
    if not gpu_uuids:
        return _empty_cost_summary()

    db_engine = connection.settings_dict.get('ENGINE', '')
    is_sqlite = 'sqlite' in db_engine

    if is_sqlite:
        interval_filter = "time >= datetime('now', ? || ' hours')"
        interval_param = [f'-{int(period_hours)}']
    else:
        interval_filter = "time >= NOW() - make_interval(hours => %s)"
        interval_param = [int(period_hours)]

    placeholders = ','.join(['%s'] * len(gpu_uuids))

    # ── Totals ────────────────────────────────────────────────────────────────
    with connection.cursor() as cur:
        cur.execute(f"""
            SELECT
                COALESCE(SUM(cost_this_period), 0),
                COALESCE(SUM(waste_this_period), 0)
            FROM cost_snapshots
            WHERE {interval_filter}
              AND gpu_uuid IN ({placeholders})
        """, interval_param + gpu_uuids)
        row = cur.fetchone()
        total_cost = float(row[0]) if row else 0.0
        total_waste = float(row[1]) if row else 0.0

    # ── By model ──────────────────────────────────────────────────────────────
    with connection.cursor() as cur:
        cur.execute(f"""
            SELECT
                COALESCE(model_name, 'unknown') AS model_name,
                COALESCE(SUM(cost_this_period), 0)  AS total_cost,
                COALESCE(SUM(waste_this_period), 0) AS total_waste,
                COUNT(*) * {INTERVAL_SECONDS} / 3600.0 AS gpu_hours
            FROM cost_snapshots
            WHERE {interval_filter}
              AND gpu_uuid IN ({placeholders})
            GROUP BY model_name
            ORDER BY total_cost DESC
        """, interval_param + gpu_uuids)
        by_model = [
            {
                "model_name": r[0],
                "total_cost": round(float(r[1]), 4),
                "total_waste": round(float(r[2]), 4),
                "gpu_hours": round(float(r[3]), 2),
            }
            for r in cur.fetchall()
        ]

    # ── By node (join via GPU table) ──────────────────────────────────────────
    node_map = {
        g.uuid: g.node.hostname
        for g in GPU.objects_unscoped.select_related('node')
        .filter(organization=org)
    }

    with connection.cursor() as cur:
        cur.execute(f"""
            SELECT
                gpu_uuid,
                COALESCE(AVG(utilization), 0)          AS avg_util,
                COALESCE(SUM(cost_this_period), 0)      AS total_cost,
                COALESCE(SUM(waste_this_period), 0)     AS total_waste
            FROM cost_snapshots
            WHERE {interval_filter}
              AND gpu_uuid IN ({placeholders})
            GROUP BY gpu_uuid
        """, interval_param + gpu_uuids)
        rows = cur.fetchall()

    # Aggregate by node hostname
    node_agg: dict = {}
    for gpu_uuid, avg_util, cost, waste in rows:
        hostname = node_map.get(gpu_uuid, 'unknown')
        if hostname not in node_agg:
            node_agg[hostname] = {'cost': 0.0, 'waste': 0.0, 'util_sum': 0.0, 'count': 0}
        node_agg[hostname]['cost'] += float(cost)
        node_agg[hostname]['waste'] += float(waste)
        node_agg[hostname]['util_sum'] += float(avg_util)
        node_agg[hostname]['count'] += 1

    by_node = [
        {
            "node_name": hn,
            "total_cost": round(d['cost'], 4),
            "total_waste": round(d['waste'], 4),
            "utilization_avg": round(d['util_sum'] / d['count'], 1) if d['count'] else 0.0,
        }
        for hn, d in sorted(node_agg.items(), key=lambda x: -x[1]['cost'])
    ]

    return {
        "total_cost": round(total_cost, 4),
        "total_waste": round(total_waste, 4),
        "by_model": by_model,
        "by_node": by_node,
    }


def get_fleet_cost_rate(org) -> float:
    """
    Return current estimated $/hr burn rate for *org*.

    Uses the most recent cost_snapshots rows (last 2 minutes) to compute
    an instantaneous rate.
    """
    gpu_uuids = list(
        GPU.objects_unscoped
        .filter(organization=org)
        .values_list('uuid', flat=True)
    )
    if not gpu_uuids:
        return 0.0

    db_engine = connection.settings_dict.get('ENGINE', '')
    is_sqlite = 'sqlite' in db_engine

    if is_sqlite:
        interval_filter = "time >= datetime('now', '-2 minutes')"
        interval_param = []
    else:
        interval_filter = "time >= NOW() - INTERVAL '2 minutes'"
        interval_param = []

    placeholders = ','.join(['%s'] * len(gpu_uuids))

    with connection.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(SUM(cost_this_period), 0)
            FROM cost_snapshots
            WHERE {interval_filter}
              AND gpu_uuid IN ({placeholders})
        """, interval_param + gpu_uuids)
        total_period_cost = float(cur.fetchone()[0])

    # cost_this_period is for INTERVAL_SECONDS; scale to $/hr
    rate_per_hr = total_period_cost * (3600.0 / INTERVAL_SECONDS)
    return round(rate_per_hr, 4)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _match_pricing(model_str: str, pricing_entries) -> Decimal | None:
    """
    Return the hourly_rate for the first pricing entry whose pattern is a
    case-insensitive substring of *model_str*.  Returns None if no match.
    """
    model_lower = model_str.lower()
    for entry in pricing_entries:
        if entry.gpu_model_pattern.lower() in model_lower:
            return entry.hourly_rate
    return None


def _ep_int_id(uuid_str: str) -> int:
    digest = hashlib.md5(uuid_str.encode()).hexdigest()
    return int(digest[:8], 16) % (2 ** 31)


def _empty_cost_summary() -> dict:
    return {
        "total_cost": 0.0,
        "total_waste": 0.0,
        "by_model": [],
        "by_node": [],
    }
