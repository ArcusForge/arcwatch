"""
Performance migration: adds missing indexes, retention policies, and compression.

The TimescaleDB-specific operations use a custom RunSQL wrapper that
skips execution on SQLite (used by the test runner).
"""
from django.db import connection, migrations


def is_postgres(schema_editor):
    return 'postgresql' in schema_editor.connection.settings_dict.get('ENGINE', '')


def run_pg_only(sql_statements):
    """Return a callable that executes SQL only on PostgreSQL."""
    def forward(apps, schema_editor):
        if not is_postgres(schema_editor):
            return
        with schema_editor.connection.cursor() as cur:
            for stmt in sql_statements:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass  # TimescaleDB functions may not exist
    return forward


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('monitor', '0011_claude_code_model'),
    ]

    operations = [
        # Add missing index on cost_snapshots (PostgreSQL only — table doesn't exist in SQLite tests)
        migrations.RunPython(
            run_pg_only([
                "CREATE INDEX IF NOT EXISTS idx_cost_gpu_uuid_time ON cost_snapshots (gpu_uuid, time DESC);",
            ]),
            reverse_code=run_pg_only([
                "DROP INDEX IF EXISTS idx_cost_gpu_uuid_time;",
            ]),
        ),

        # Retention policies (TimescaleDB only)
        migrations.RunPython(
            run_pg_only([
                "SELECT add_retention_policy('gpu_metrics', INTERVAL '90 days', if_not_exists => true);",
                "SELECT add_retention_policy('inference_metrics', INTERVAL '90 days', if_not_exists => true);",
                "SELECT add_retention_policy('cost_snapshots', INTERVAL '365 days', if_not_exists => true);",
            ]),
            reverse_code=noop,
        ),

        # Compression policies (TimescaleDB only)
        migrations.RunPython(
            run_pg_only([
                "ALTER TABLE gpu_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'gpu_uuid');",
                "SELECT add_compression_policy('gpu_metrics', INTERVAL '7 days', if_not_exists => true);",
                "ALTER TABLE cost_snapshots SET (timescaledb.compress, timescaledb.compress_segmentby = 'gpu_uuid');",
                "SELECT add_compression_policy('cost_snapshots', INTERVAL '7 days', if_not_exists => true);",
            ]),
            reverse_code=noop,
        ),
    ]
