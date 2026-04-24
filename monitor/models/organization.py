"""
monitor/models/organization.py -- Organization, Team, UserProfile, and APIKey models.
"""
import datetime
import hashlib
import secrets
import uuid

from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


# ── Organization ──────────────────────────────────────────────────────────────

class Organization(models.Model):
    """
    Top-level tenant. All GPU resources belong to an organization.
    """
    PLAN_CHOICES = [
        ('free', 'Free'),
        ('pro', 'Pro'),
        ('enterprise', 'Enterprise'),
    ]

    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, max_length=100)
    owner = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='owned_orgs',
    )
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default='free')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['owner']),
            models.Index(fields=['plan']),
        ]

    def __str__(self):
        return self.name

    def get_members(self):
        return User.objects.filter(profile__organization=self)


# ── Team ─────────────────────────────────────────────────────────────────────

class Team(models.Model):
    """
    A sub-group within an organization, used for cost attribution and RBAC scoping.
    """
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='teams',
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100)
    cost_center = models.CharField(
        max_length=100, blank=True, default='',
        help_text='Billing / chargeback cost center identifier',
    )

    class Meta:
        ordering = ['organization', 'name']
        unique_together = [('organization', 'slug')]
        indexes = [
            models.Index(fields=['organization']),
        ]

    def __str__(self):
        return f"{self.organization.slug}/{self.slug}"


# ── UserProfile ───────────────────────────────────────────────────────────────

class UserProfile(models.Model):
    """
    Extends Django's built-in User with org membership and role.
    Created automatically via post_save signal on User.
    """
    ROLE_CHOICES = [
        ('viewer', 'Viewer'),       # read-only
        ('operator', 'Operator'),   # can acknowledge alerts, view metrics
        ('admin', 'Admin'),         # can manage org settings and members
        ('owner', 'Owner'),         # full control, billing
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    organization = models.ForeignKey(
        Organization, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='members',
    )
    team = models.ForeignKey(
        Team, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='members',
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='viewer')

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'role']),
        ]

    def __str__(self):
        return f"Profile({self.user.username})"


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, created, **kwargs):
    """No-op: profile is created by create_user_profile above. Avoid re-save loops."""
    pass


# ── APIKey ────────────────────────────────────────────────────────────────────

class APIKey(models.Model):
    """
    An API key for programmatic access (Go agent, CI pipelines, etc.).
    The raw key is shown once at creation; only a SHA-256 hash is stored.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='api_keys',
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='api_keys',
    )
    name = models.CharField(max_length=255)
    # SHA-256 hash of the full key (never store the raw key)
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    # First 8 chars of the raw key (safe to display; used for human identification)
    key_prefix = models.CharField(max_length=8)
    # Permissions granted by this key, e.g. ["ingest", "read"]
    scopes = models.JSONField(default=list)
    active = models.BooleanField(default=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'active']),
        ]

    def __str__(self):
        return f"{self.name} ({self.key_prefix}…)"

    @classmethod
    def create_key(cls, organization, user, name, scopes=None, expires_at=None):
        """
        Generate a new API key, store its hash, and return (APIKey instance, raw_key).
        The raw key is NOT stored and cannot be recovered after this call.
        """
        if scopes is None:
            scopes = ['ingest']
        raw_key = secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_prefix = raw_key[:8]
        api_key = cls.objects.create(
            organization=organization,
            user=user,
            name=name,
            key_hash=key_hash,
            key_prefix=key_prefix,
            scopes=scopes,
            expires_at=expires_at,
        )
        return api_key, raw_key

    @classmethod
    def authenticate(cls, raw_key):
        """
        Look up a key by its SHA-256 hash. Returns the APIKey if valid and active,
        or None if the key does not exist, is inactive, or has expired.
        """
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        try:
            api_key = cls.objects.select_related('organization', 'user').get(
                key_hash=key_hash, active=True,
            )
        except cls.DoesNotExist:
            return None
        if api_key.expires_at and api_key.expires_at < timezone.now():
            return None
        # Update last_used_at without triggering model signals
        cls.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now())
        api_key.refresh_from_db(fields=['last_used_at'])
        return api_key


# ── Invite ─────────────────────────────────────────────────────────────────

class Invite(models.Model):
    """
    Email-based invitation for a new member to join an organization.
    Token is a UUID used as the one-time accept link. Expires after 7 days.
    """
    ROLE_CHOICES = [
        ('viewer', 'Viewer'),
        ('operator', 'Operator'),
        ('admin', 'Admin'),
        ('owner', 'Owner'),
    ]

    token = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='invites',
    )
    invited_by = models.ForeignKey(
        User, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='sent_invites',
    )
    email = models.EmailField()
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='viewer')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = [('organization', 'email')]
        indexes = [
            models.Index(fields=['organization', 'accepted_at']),
            models.Index(fields=['email']),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding and not self.expires_at:
            self.expires_at = timezone.now() + datetime.timedelta(days=7)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Invite({self.email} → {self.organization.slug})"

    @property
    def is_expired(self):
        if self.expires_at is None:
            return False
        return timezone.now() > self.expires_at

    @property
    def is_accepted(self):
        return self.accepted_at is not None

    @property
    def is_pending(self):
        return not self.is_accepted and not self.is_expired
