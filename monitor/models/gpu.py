"""
monitor/models/gpu.py -- GPU hardware inventory: clusters, nodes, and individual GPU devices.
"""
import uuid

from django.db import models
from django.utils import timezone

from .base import TenantManager


# ── GPUCluster ────────────────────────────────────────────────────────────────

class GPUCluster(models.Model):
    """
    A logical grouping of GPU nodes (e.g. a Kubernetes cluster or on-prem rack).
    """
    CLOUD_CHOICES = [
        ('aws', 'Amazon Web Services'),
        ('gcp', 'Google Cloud Platform'),
        ('azure', 'Microsoft Azure'),
        ('coreweave', 'CoreWeave'),
        ('lambda', 'Lambda Labs'),
        ('on_prem', 'On-Premises'),
        ('other', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'monitor.Organization', on_delete=models.CASCADE,
        related_name='gpu_clusters', db_index=True,
    )
    name = models.CharField(max_length=255)
    cloud = models.CharField(max_length=20, choices=CLOUD_CHOICES, default='other')
    region = models.CharField(max_length=100, blank=True, default='')
    k8s_context = models.CharField(
        max_length=255, blank=True, default='',
        help_text='kubectl context name for this cluster',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True, db_index=True)

    objects = TenantManager()
    objects_unscoped = models.Manager()

    class Meta:
        ordering = ['organization', 'name']
        unique_together = [('organization', 'name')]
        indexes = [
            models.Index(fields=['organization', 'cloud']),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_cloud_display()})"


# ── GPUNode ───────────────────────────────────────────────────────────────────

class GPUNode(models.Model):
    """
    A physical or virtual machine that hosts one or more GPUs.
    Registered automatically when the Go agent first reports from a new host.
    """
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('degraded', 'Degraded'),
        ('offline', 'Offline'),
        ('draining', 'Draining'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cluster = models.ForeignKey(
        GPUCluster, on_delete=models.CASCADE,
        related_name='nodes', db_index=True,
    )
    organization = models.ForeignKey(
        'monitor.Organization', on_delete=models.CASCADE,
        related_name='gpu_nodes', db_index=True,
    )
    hostname = models.CharField(max_length=255, db_index=True)
    instance_type = models.CharField(max_length=100, blank=True, default='',
                                     help_text='Cloud instance type, e.g. p4d.24xlarge')
    gpu_count = models.PositiveSmallIntegerField(default=0)
    gpu_type = models.CharField(max_length=200, blank=True, default='',
                                help_text='GPU model string, e.g. NVIDIA A100-SXM4-80GB')
    gpu_memory_gb = models.PositiveSmallIntegerField(null=True, blank=True)
    hourly_cost = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True,
        help_text='$/hr for the entire node (from cloud pricing or user override)',
    )

    # Kubernetes metadata (nullable for bare-metal)
    k8s_node_name = models.CharField(max_length=255, blank=True, default='')
    k8s_labels = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='active', db_index=True,
    )
    last_seen = models.DateTimeField(default=timezone.now, db_index=True)
    agent_version = models.CharField(max_length=50, blank=True, default='')
    is_active = models.BooleanField(default=True, db_index=True)

    objects = TenantManager()
    objects_unscoped = models.Manager()

    class Meta:
        ordering = ['-last_seen']
        unique_together = [('organization', 'hostname')]
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['cluster', 'hostname']),
        ]

    def __str__(self):
        return f"{self.hostname} ({self.gpu_count} GPUs)"


# ── GPU ───────────────────────────────────────────────────────────────────────

class GPU(models.Model):
    """
    An individual GPU device attached to a GPUNode.
    Updated from agent reports every scrape interval.
    """
    STATUS_CHOICES = [
        ('healthy', 'Healthy'),
        ('degraded', 'Degraded'),       # ECC errors, thermal throttling
        ('unreachable', 'Unreachable'),  # No recent metrics
        ('retired', 'Retired'),          # Manually decommissioned
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    node = models.ForeignKey(
        GPUNode, on_delete=models.CASCADE,
        related_name='gpus', db_index=True,
    )
    organization = models.ForeignKey(
        'monitor.Organization', on_delete=models.CASCADE,
        related_name='gpus', db_index=True,
    )
    gpu_index = models.SmallIntegerField(
        help_text='nvidia-smi device index (0, 1, 2, ...)',
    )
    uuid = models.CharField(
        max_length=64, unique=True,
        help_text='NVIDIA GPU UUID (GPU-xxxxxxxx-xxxx-...)',
    )

    # Latest denormalized snapshot values (written by agent on each scrape)
    current_utilization = models.FloatField(null=True, blank=True,
                                            help_text='GPU utilization % (0-100)')
    current_memory_used_mb = models.IntegerField(null=True, blank=True)
    current_memory_total_mb = models.IntegerField(null=True, blank=True)
    current_temperature_c = models.SmallIntegerField(null=True, blank=True)
    current_power_watts = models.FloatField(null=True, blank=True)
    current_clock_mhz = models.IntegerField(null=True, blank=True)
    current_model_name = models.CharField(max_length=200, blank=True, default='')

    # Optional FK to the inference endpoint currently running on this GPU
    current_endpoint_id = models.ForeignKey(
        'monitor.InferenceEndpoint', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='current_gpus',
    )
    current_k8s_pod = models.CharField(max_length=255, blank=True, default='')

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='healthy', db_index=True,
    )
    ecc_errors = models.IntegerField(default=0)
    last_updated = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()
    objects_unscoped = models.Manager()

    class Meta:
        ordering = ['node', 'gpu_index']
        unique_together = [('node', 'gpu_index')]
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['node', 'status']),
            models.Index(fields=['uuid']),
        ]

    def __str__(self):
        name = self.current_model_name or 'GPU'
        return f"{name} [{self.gpu_index}] on {self.node.hostname}"

    @property
    def memory_utilization_pct(self):
        """Return memory utilization as a percentage, or None if data is unavailable."""
        if self.current_memory_used_mb is not None and self.current_memory_total_mb is not None and self.current_memory_total_mb > 0:
            return round(self.current_memory_used_mb / self.current_memory_total_mb * 100, 1)
        return None

    @property
    def is_idle(self):
        """True when GPU utilization is below 5% and status is healthy."""
        return (
            self.status == 'healthy'
            and self.current_utilization is not None
            and self.current_utilization < 5.0
        )
