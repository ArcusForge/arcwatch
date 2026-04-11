"""
monitor/views/settings_views.py -- Settings management views (API Keys, Alert Rules, Resources, Members).
"""
from django.conf import settings as django_settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User as DjangoUser
from django.core.mail import send_mail
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone as tz

from monitor.decorators import is_htmx, require_admin, require_operator
from monitor.forms import (
    APIKeyCreateForm, AlertRuleForm, InviteForm,
    GPUClusterForm, InferenceEndpointForm, AcceptInviteForm,
)
from monitor.models import APIKey, AlertRule, Invite, GPUCluster, GPUNode, InferenceEndpoint, LLMProvider
from monitor.services.llm_sync_engine import encrypt_api_key, sync_provider, sync_claude_code


# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_org(user):
    """Return the organization for the logged-in user, or None."""
    try:
        return user.profile.organization
    except Exception:
        return None


def _is_admin(user):
    """Return True if the user has admin or owner role."""
    try:
        return user.profile.role in ('admin', 'owner')
    except Exception:
        return False


# ── Settings root ─────────────────────────────────────────────────────────────

@login_required
def settings_root(request):
    return redirect('monitor:settings_api_keys')


# ── API Keys ──────────────────────────────────────────────────────────────────

@login_required
def settings_api_keys(request):
    org = _get_org(request.user)
    is_admin = _is_admin(request.user)
    new_raw_key = None

    if request.method == 'POST':
        # Defense-in-depth: both the _is_admin() helper and an inline role
        # tuple check are enforced for write requests. If _is_admin is ever
        # accidentally loosened, the explicit role check still blocks.
        try:
            role_ok = request.user.profile.role in ('admin', 'owner')
        except AttributeError:
            role_ok = False
        if not is_admin or not role_ok:
            return HttpResponseForbidden("Admin access required.")
        form = APIKeyCreateForm(request.POST)
        if form.is_valid():
            _, new_raw_key = APIKey.create_key(
                organization=org,
                user=request.user,
                name=form.cleaned_data['name'],
                scopes=form.cleaned_data['scopes'],
            )
    else:
        form = APIKeyCreateForm()

    return render(request, 'monitor/settings_api_keys.html', {
        'active_tab': 'api-keys',
        'org': org,
        'is_admin': is_admin,
        'api_keys': org.api_keys.all() if org else [],
        'form': form,
        'new_raw_key': new_raw_key,
    })


@login_required
@require_admin
def revoke_api_key(request, key_id):
    org = _get_org(request.user)
    api_key = get_object_or_404(APIKey, pk=key_id, organization=org)
    if request.method == 'POST':
        api_key.active = False
        api_key.save(update_fields=['active'])
    if is_htmx(request):
        return render(request, 'monitor/fragments/api_key_row.html', {
            'key': api_key, 'is_admin': True,
        })
    return redirect('monitor:settings_api_keys')


# ── Alert Rules ───────────────────────────────────────────────────────────────

@login_required
def settings_alert_rules(request):
    org = _get_org(request.user)
    return render(request, 'monitor/settings_alert_rules.html', {
        'active_tab': 'alert-rules',
        'org': org,
        'is_admin': _is_admin(request.user),
        'rules': org.alert_rules.all() if org else [],
        'form': AlertRuleForm(),
    })


@login_required
@require_operator
def create_alert_rule(request):
    if request.method != 'POST':
        return redirect('monitor:settings_alert_rules')
    org = _get_org(request.user)
    if org is None:
        return HttpResponseForbidden("No organization.")
    form = AlertRuleForm(request.POST)
    if form.is_valid():
        rule = form.save(commit=False)
        rule.organization = org
        rule.save()
        return redirect('monitor:settings_alert_rules')
    # Re-render page with form errors
    return render(request, 'monitor/settings_alert_rules.html', {
        'active_tab': 'alert-rules',
        'org': org,
        'is_admin': True,
        'rules': org.alert_rules.all(),
        'form': form,
        'show_form': True,
    })


@login_required
@require_operator
def toggle_alert_rule(request, rule_id):
    if request.method != 'POST':
        return HttpResponse(status=405)
    org = _get_org(request.user)
    rule = get_object_or_404(AlertRule, pk=rule_id, organization=org)
    rule.is_enabled = not rule.is_enabled
    rule.save(update_fields=['is_enabled'])
    return render(request, 'monitor/fragments/alert_rule_toggle.html', {
        'rule': rule, 'is_admin': _is_admin(request.user),
    })


@login_required
@require_admin
def delete_alert_rule(request, rule_id):
    if request.method != 'POST':
        return HttpResponse(status=405)
    org = _get_org(request.user)
    rule = get_object_or_404(AlertRule, pk=rule_id, organization=org)
    rule.delete()
    return HttpResponse('')


# ── Resources ─────────────────────────────────────────────────────────────────

@login_required
def settings_resources(request):
    org = _get_org(request.user)
    tab = request.GET.get('tab', 'clusters')
    clusters = org.gpu_clusters.filter(is_active=True).prefetch_related('nodes') if org else []
    endpoints = org.inference_endpoints.filter(is_active=True) if org else []
    return render(request, 'monitor/settings_resources.html', {
        'active_tab': 'resources',
        'tab': tab,
        'org': org,
        'is_admin': _is_admin(request.user),
        'clusters': clusters,
        'endpoints': endpoints,
    })


@login_required
@require_admin
def create_cluster(request):
    if request.method != 'POST':
        return redirect('monitor:settings_resources')
    org = _get_org(request.user)
    if org is None:
        return HttpResponseForbidden("No organization.")
    form = GPUClusterForm(request.POST)
    if form.is_valid():
        GPUCluster.objects_unscoped.create(organization=org, name=form.cleaned_data['name'])
    return redirect('monitor:settings_resources')


@login_required
@require_admin
def deactivate_cluster(request, cluster_id):
    org = _get_org(request.user)
    cluster = get_object_or_404(GPUCluster, pk=cluster_id, organization=org)
    if request.method == 'POST':
        cluster.is_active = False
        cluster.save(update_fields=['is_active'])
    return HttpResponse('<span style="background:rgba(100,116,139,.1);border:1px solid #475569;color:#64748b;font-size:.62rem;padding:2px 7px;border-radius:10px">inactive</span>')


@login_required
@require_admin
def delete_cluster(request, cluster_id):
    org = _get_org(request.user)
    cluster = get_object_or_404(GPUCluster, pk=cluster_id, organization=org)
    if request.method == 'POST':
        cluster.delete()
    return HttpResponse('')


@login_required
@require_admin
def deactivate_node(request, node_id):
    if request.method != 'POST':
        return HttpResponse(status=405)
    org = _get_org(request.user)
    node = get_object_or_404(GPUNode, pk=node_id, organization=org)
    node.is_active = False
    node.save(update_fields=['is_active'])
    return HttpResponse('<span style="background:rgba(100,116,139,.1);border:1px solid #475569;color:#64748b;font-size:.62rem;padding:2px 7px;border-radius:10px">inactive</span>')


@login_required
@require_admin
def delete_node(request, node_id):
    if request.method != 'POST':
        return HttpResponse(status=405)
    org = _get_org(request.user)
    node = get_object_or_404(GPUNode, pk=node_id, organization=org)
    node.delete()
    return HttpResponse('')


@login_required
@require_admin
def create_endpoint(request):
    if request.method != 'POST':
        return redirect(f"{reverse('monitor:settings_resources')}?tab=endpoints")
    org = _get_org(request.user)
    form = InferenceEndpointForm(request.POST)
    if form.is_valid():
        InferenceEndpoint.objects.create(
            organization=org,
            name=form.cleaned_data['name'],
            engine=form.cleaned_data['engine'],
            url=form.cleaned_data.get('url', ''),
        )
    return redirect(f"{reverse('monitor:settings_resources')}?tab=endpoints")


@login_required
@require_admin
def deactivate_endpoint(request, endpoint_id):
    org = _get_org(request.user)
    ep = get_object_or_404(InferenceEndpoint, pk=endpoint_id, organization=org)
    if request.method == 'POST':
        ep.is_active = False
        ep.save(update_fields=['is_active'])
    return HttpResponse('<span style="background:rgba(100,116,139,.1);border:1px solid #475569;color:#64748b;font-size:.62rem;padding:2px 7px;border-radius:10px">retired</span>')


@login_required
@require_admin
def delete_endpoint(request, endpoint_id):
    org = _get_org(request.user)
    ep = get_object_or_404(InferenceEndpoint, pk=endpoint_id, organization=org)
    if request.method == 'POST':
        ep.delete()
    return HttpResponse('')


# ── Members ───────────────────────────────────────────────────────────────────

@login_required
def settings_members(request):
    org = _get_org(request.user)
    members = org.get_members().select_related('profile') if org else []
    pending = Invite.objects.filter(organization=org, accepted_at__isnull=True) if org else []
    return render(request, 'monitor/settings_members.html', {
        'active_tab': 'members', 'org': org, 'is_admin': _is_admin(request.user),
        'members': members, 'pending_invites': pending, 'form': InviteForm(),
    })


@login_required
@require_admin
def change_member_role(request, user_id):
    if request.method != 'POST':
        return HttpResponse(status=405)
    org = _get_org(request.user)
    member = get_object_or_404(DjangoUser, pk=user_id, profile__organization=org)
    role = request.POST.get('role', '')
    if role == 'owner' and request.user.profile.role != 'owner':
        return HttpResponseForbidden("Only owners can assign the owner role.")
    if role in ('viewer', 'operator', 'admin', 'owner'):
        member.profile.role = role
        member.profile.save(update_fields=['role'])
    role_colors = {
        'owner': '#76B900', 'admin': '#fbbf24',
        'operator': '#a78bfa', 'viewer': '#60a5fa',
    }
    color = role_colors.get(member.profile.role, '#94a3b8')
    return HttpResponse(
        f'<span style="background:rgba(0,0,0,.1);border:1px solid {color}40;color:{color};'
        f'font-size:.62rem;padding:2px 7px;border-radius:10px;cursor:pointer">'
        f'{member.profile.role} &#9662;</span>'
    )


@login_required
@require_admin
def remove_member(request, user_id):
    if request.method != 'POST':
        return HttpResponse(status=405)
    org = _get_org(request.user)
    member = get_object_or_404(DjangoUser, pk=user_id, profile__organization=org)
    if member != request.user:
        member.profile.organization = None
        member.profile.save(update_fields=['organization'])
    return HttpResponse('')


@login_required
@require_admin
def invite_member(request):
    if request.method != 'POST':
        return redirect('monitor:settings_members')
    org = _get_org(request.user)
    if org is None:
        return HttpResponseForbidden("No organization.")
    form = InviteForm(request.POST)
    if form.is_valid():
        invite = Invite.objects.create(
            organization=org,
            invited_by=request.user,
            email=form.cleaned_data['email'],
            role=form.cleaned_data['role'],
        )
        accept_url = request.build_absolute_uri(f'/accounts/accept-invite/{invite.token}/')
        send_mail(
            subject=f"You're invited to {org.name} on ArcWatch",
            message=(
                f"Hi,\n\n"
                f"{request.user.username} has invited you to join {org.name} on ArcWatch "
                f"as a {invite.role}.\n\n"
                f"Accept your invite here (expires in 7 days):\n{accept_url}\n\n"
                f"— The ArcWatch Team"
            ),
            from_email=getattr(django_settings, 'DEFAULT_FROM_EMAIL', 'noreply@arcwatch.local'),
            recipient_list=[invite.email],
            fail_silently=True,
        )
    return redirect('monitor:settings_members')


@login_required
@require_admin
def revoke_invite(request, token):
    if request.method != 'POST':
        return HttpResponse(status=405)
    org = _get_org(request.user)
    invite = get_object_or_404(Invite, token=token, organization=org, accepted_at__isnull=True)
    invite.delete()
    return HttpResponse('')


@login_required
@require_admin
def resend_invite(request, token):
    if request.method != 'POST':
        return HttpResponse(status=405)
    org = _get_org(request.user)
    invite = get_object_or_404(Invite, token=token, organization=org, accepted_at__isnull=True)
    accept_url = request.build_absolute_uri(f'/accounts/accept-invite/{invite.token}/')
    send_mail(
        subject=f"You're invited to {org.name} on ArcWatch (reminder)",
        message=f"Accept your invite here (expires in 7 days):\n{accept_url}",
        from_email=getattr(django_settings, 'DEFAULT_FROM_EMAIL', 'noreply@arcwatch.local'),
        recipient_list=[invite.email],
        fail_silently=True,
    )
    return HttpResponse('<span style="color:#60a5fa;font-size:.68rem">Sent &#10003;</span>')


# ── Accept invite (no login required — used by new users) ────────────────────

def accept_invite(request, token):
    invite = get_object_or_404(Invite, token=token)

    if invite.is_accepted:
        return redirect('login')

    if invite.is_expired:
        return render(request, 'monitor/accept_invite.html', {
            'error': "This invite has expired. Ask an admin to resend it.",
            'invite': invite,
        })

    if request.method == 'POST':
        form = AcceptInviteForm(request.POST)
        if form.is_valid():
            if DjangoUser.objects.filter(username=form.cleaned_data['username']).exists():
                return render(request, 'monitor/accept_invite.html', {
                    'invite': invite, 'form': form,
                    'error': 'Username already taken. Please choose another.',
                })
            user = DjangoUser.objects.create_user(
                username=form.cleaned_data['username'],
                email=invite.email,
                password=form.cleaned_data['password'],
            )
            user.profile.organization = invite.organization
            user.profile.role = invite.role
            user.profile.save()
            invite.accepted_at = tz.now()
            invite.save(update_fields=['accepted_at'])
            login(request, user)
            return redirect('monitor:gpu_fleet_dashboard')
    else:
        form = AcceptInviteForm()

    return render(request, 'monitor/accept_invite.html', {
        'form': form,
        'invite': invite,
    })


# ── LLM Providers ─────────────────────────────────────────────────────────────

@login_required
def settings_llm_providers(request):
    org = _get_org(request.user)
    synced_raw = request.GET.get("synced")
    try:
        synced = int(synced_raw) if synced_raw is not None else None
    except (ValueError, TypeError):
        synced = None
    error = request.GET.get("error")
    providers = org.llm_providers.all() if org else []
    return render(request, "monitor/settings_llm_providers.html", {
        "active_tab": "llm-providers",
        "org": org,
        "is_admin": _is_admin(request.user),
        "providers": providers,
        "synced": synced,
        "error": error,
    })


@login_required
@require_admin
def create_llm_provider(request):
    if request.method != "POST":
        return redirect("monitor:settings_llm_providers")
    org = _get_org(request.user)
    if org is None:
        return HttpResponseForbidden("No organization.")
    raw_key = request.POST.get("api_key", "").strip()
    provider = request.POST.get("provider", "").strip()
    label = request.POST.get("label", "").strip()
    if not raw_key or not provider or not label:
        return redirect("monitor:settings_llm_providers")
    valid_providers = {c[0] for c in LLMProvider.PROVIDER_CHOICES}
    if provider not in valid_providers:
        return redirect("monitor:settings_llm_providers")
    LLMProvider.objects.create(
        organization=org,
        provider=provider,
        label=label,
        api_key_encrypted=encrypt_api_key(raw_key),
    )
    return redirect("monitor:settings_llm_providers")


@login_required
@require_admin
def delete_llm_provider(request, provider_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    org = _get_org(request.user)
    p = get_object_or_404(LLMProvider, pk=provider_id, organization=org)
    p.delete()
    return HttpResponse("")


@login_required
@require_admin
def toggle_llm_provider(request, provider_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    org = _get_org(request.user)
    p = get_object_or_404(LLMProvider, pk=provider_id, organization=org)
    p.is_active = not p.is_active
    p.save(update_fields=["is_active"])
    color = "#4ade80" if p.is_active else "#64748b"
    label = "active" if p.is_active else "inactive"
    return HttpResponse(
        f'<span style="background:rgba(0,0,0,.1);border:1px solid {color}40;color:{color};'
        f'font-size:.62rem;padding:2px 7px;border-radius:10px">{label}</span>'
    )


@login_required
@require_admin
def sync_llm_provider(request, provider_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    org = _get_org(request.user)
    p = get_object_or_404(LLMProvider, pk=provider_id, organization=org)
    try:
        count = sync_provider(str(p.pk))
        # Also pull Claude Code analytics if this is an Anthropic key
        if p.provider == "anthropic":
            try:
                sync_claude_code(str(p.pk))
            except Exception as cc_exc:
                import logging
                logging.getLogger(__name__).warning("Claude Code sync skipped: %s", cc_exc)
        from django.urls import reverse
        return redirect(f"{reverse('monitor:settings_llm_providers')}?synced={count}")
    except Exception as exc:
        import urllib.parse
        from django.urls import reverse
        return redirect(f"{reverse('monitor:settings_llm_providers')}?error={urllib.parse.quote(str(exc)[:120])}")
