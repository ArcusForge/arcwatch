"""
monitor/management/commands/seed_demo_data.py

CLI wrapper around monitor.services.demo_seeder.seed_demo_fleet().
Creates the legacy demo user + demo-org and seeds the demo fleet into it.

Usage:
    python manage.py seed_demo_data --nodes 4 --gpus-per-node 4 --hours 6
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from monitor.models import Organization
from monitor.services.demo_seeder import seed_demo_fleet


class Command(BaseCommand):
    help = "Seed demo GPU telemetry, inference endpoints, cost, and alert data."

    def add_arguments(self, parser):
        parser.add_argument("--nodes", type=int, default=4)
        parser.add_argument("--gpus-per-node", type=int, default=4)
        parser.add_argument("--hours", type=int, default=6)
        parser.add_argument("--cluster-name", type=str, default="demo-cluster",
                            help="Cluster name for the demo fleet (default: demo-cluster)")

    def handle(self, *args, **options):
        node_count = options["nodes"]
        gpus_per_node = options["gpus_per_node"]
        hours = options["hours"]
        cluster_name = options["cluster_name"]

        self.stdout.write(self.style.MIGRATE_HEADING("GPUWatch Demo Data Seeder"))

        # Demo user + org (CLI-only convenience; the service itself just needs an org)
        demo_user, user_created = User.objects.get_or_create(
            username="demo",
            defaults={"email": "demo@gpuwatch.dev", "is_staff": False},
        )
        if user_created:
            demo_user.set_password("demo")
            demo_user.save()
            self.stdout.write(self.style.SUCCESS("  Created user: demo / demo"))

        org, org_created = Organization.objects.get_or_create(
            slug="demo-org",
            defaults={
                "name": "Demo Organization",
                "owner": demo_user,
                "plan": "pro",
            },
        )
        if org_created:
            self.stdout.write(self.style.SUCCESS(f"  Created org: {org.name}"))

        try:
            result = seed_demo_fleet(
                org=org,
                user=demo_user,
                nodes=node_count,
                gpus_per_node=gpus_per_node,
                hours=hours,
                cluster_name=cluster_name,
                log=lambda msg: self.stdout.write(msg),
            )
        except ValueError as exc:
            raise CommandError(str(exc))

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Seed complete!"))
        for k, v in result.items():
            self.stdout.write(f"  {k:<24}: {v}")
        self.stdout.write("")
        self.stdout.write("  Login:     demo / demo")
        self.stdout.write("  Dashboard: http://localhost:8000/")
