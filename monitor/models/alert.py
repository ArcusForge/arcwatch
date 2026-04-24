"""
monitor/models/alert.py -- Alert rules and triggered alert events.
"""
import re

from django.core.exceptions import ValidationError
from django.db import models


def validate_slack_webhook_url(value):
    """Ensure the webhook URL is a Slack webhook to prevent SSRF."""
    if value and not re.match(r'^https://hooks\.slack\.com/', value):
        raise ValidationError('Webhook URL must be a Slack webhook (https://hooks.slack.com/...)')


class AlertRule(models.Model):
    """
    A threshold-based alert rule owned by an organization.
    """
    METRIC_CHOICES = [
        ('gpu_utilization_low', 'GPU Underutilization'),
        ('gpu_memory_high', 'GPU Memory High'),
        ('latency_high', 'Latency High'),
        ('gpu_offline', 'GPU Offline'),
        ('cost_anomaly', 'Cost Anomaly'),
    ]

    organization = models.ForeignKey(
        'monitor.Organization', on_delete=models.CASCADE,
        related_name='alert_rules',
    )
    name = models.CharField(max_length=255)
    metric = models.CharField(max_length=64, choices=METRIC_CHOICES)
    threshold_value = models.FloatField(
        help_text='Trigger when metric exceeds (or falls below) this value',
    )
    duration_seconds = models.IntegerField(
        default=300,
        help_text='Metric must be out-of-bounds for this many seconds before firing',
    )
    is_enabled = models.BooleanField(default=True)
    slack_webhook_url = models.URLField(blank=True, default='', validators=[validate_slack_webhook_url])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['organization', 'name']
        indexes = [
            models.Index(fields=['organization', 'is_enabled']),
        ]

    def __str__(self):
        return f"{self.name} [{self.get_metric_display()}]"


class AlertEvent(models.Model):
    """
    A single firing (and optional resolution) of an AlertRule.
    """
    SEVERITY_CHOICES = [
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('critical', 'Critical'),
    ]

    rule = models.ForeignKey(
        AlertRule, on_delete=models.CASCADE,
        related_name='events',
    )
    triggered_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    severity = models.CharField(
        max_length=16, choices=SEVERITY_CHOICES, default='warning',
    )
    message = models.TextField()
    context = models.JSONField(default=dict)
    notification_sent = models.BooleanField(default=False)

    class Meta:
        ordering = ['-triggered_at']
        indexes = [
            models.Index(fields=['rule', 'triggered_at']),
        ]

    def __str__(self):
        return f"Alert({self.rule.name}) @ {self.triggered_at:%Y-%m-%d %H:%M}"

    @property
    def is_active(self):
        return self.resolved_at is None
