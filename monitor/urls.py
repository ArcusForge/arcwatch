from django.urls import path

from monitor.rest_api import ingest_gpu, ingest_inference
from monitor.views.health_views import healthz, readyz
from monitor.views.dashboard_views import gpu_fleet_dashboard, landing
from monitor.views.inference_views import inference_dashboard
from monitor.views.cost_views import cost_dashboard
from monitor.views.alert_views import alerts_dashboard
from monitor.views.llm_views import llm_dashboard, llm_setup, claude_code_dashboard
from monitor.views.onboarding_views import no_organization
from monitor.views.settings_views import (
    settings_root, settings_api_keys, settings_alert_rules,
    settings_resources, settings_members, revoke_api_key,
    create_alert_rule, toggle_alert_rule, delete_alert_rule,
    create_cluster, deactivate_cluster, delete_cluster,
    deactivate_node, delete_node,
    create_endpoint, deactivate_endpoint, delete_endpoint,
    change_member_role, remove_member, invite_member,
    revoke_invite, resend_invite,
    settings_llm_providers, create_llm_provider, delete_llm_provider,
    toggle_llm_provider, sync_llm_provider,
)

app_name = 'monitor'

urlpatterns = [
    # ── Health checks (no auth required) ─────────────────────────────────────
    path('api/health/', healthz, name='healthz'),
    path('api/ready/', readyz, name='readyz'),

    # ── Dashboard views ───────────────────────────────────────────────────────
    path('', landing, name='landing'),
    path('dashboard/', gpu_fleet_dashboard, name='gpu_fleet_dashboard'),
    path('inference/', inference_dashboard, name='inference_dashboard'),
    path('costs/', cost_dashboard, name='cost_dashboard'),
    path('alerts/', alerts_dashboard, name='alerts_dashboard'),
    path('llm/', llm_dashboard, name='llm_dashboard'),
    path('llm/setup/', llm_setup, name='llm_setup'),
    path('claude-code/', claude_code_dashboard, name='claude_code_dashboard'),
    path('no-organization/', no_organization, name='no_organization'),

    # ── Settings views ────────────────────────────────────────────────────────
    path('settings/', settings_root, name='settings_root'),
    path('settings/api-keys/', settings_api_keys, name='settings_api_keys'),
    path('settings/api-keys/<uuid:key_id>/revoke/', revoke_api_key, name='revoke_api_key'),
    path('settings/alert-rules/', settings_alert_rules, name='settings_alert_rules'),
    path('settings/alert-rules/create/', create_alert_rule, name='create_alert_rule'),
    path('settings/alert-rules/<int:rule_id>/toggle/', toggle_alert_rule, name='toggle_alert_rule'),
    path('settings/alert-rules/<int:rule_id>/delete/', delete_alert_rule, name='delete_alert_rule'),
    path('settings/resources/', settings_resources, name='settings_resources'),
    path('settings/resources/clusters/create/', create_cluster, name='create_cluster'),
    path('settings/resources/clusters/<uuid:cluster_id>/deactivate/', deactivate_cluster, name='deactivate_cluster'),
    path('settings/resources/clusters/<uuid:cluster_id>/delete/', delete_cluster, name='delete_cluster'),
    path('settings/resources/nodes/<uuid:node_id>/deactivate/', deactivate_node, name='deactivate_node'),
    path('settings/resources/nodes/<uuid:node_id>/delete/', delete_node, name='delete_node'),
    path('settings/resources/endpoints/create/', create_endpoint, name='create_endpoint'),
    path('settings/resources/endpoints/<uuid:endpoint_id>/deactivate/', deactivate_endpoint, name='deactivate_endpoint'),
    path('settings/resources/endpoints/<uuid:endpoint_id>/delete/', delete_endpoint, name='delete_endpoint'),
    path('settings/members/', settings_members, name='settings_members'),
    path('settings/members/<int:user_id>/role/', change_member_role, name='change_member_role'),
    path('settings/members/<int:user_id>/remove/', remove_member, name='remove_member'),
    path('settings/members/invite/', invite_member, name='invite_member'),
    path('settings/members/invite/<uuid:token>/revoke/', revoke_invite, name='revoke_invite'),
    path('settings/members/invite/<uuid:token>/resend/', resend_invite, name='resend_invite'),
    path('settings/llm-providers/', settings_llm_providers, name='settings_llm_providers'),
    path('settings/llm-providers/create/', create_llm_provider, name='create_llm_provider'),
    path('settings/llm-providers/<uuid:provider_id>/delete/', delete_llm_provider, name='delete_llm_provider'),
    path('settings/llm-providers/<uuid:provider_id>/toggle/', toggle_llm_provider, name='toggle_llm_provider'),
    path('settings/llm-providers/<uuid:provider_id>/sync/', sync_llm_provider, name='sync_llm_provider'),

    # ── REST API ──────────────────────────────────────────────────────────────
    path('api/v1/ingest/gpu/', ingest_gpu, name='api_ingest_gpu'),
    path('api/v1/ingest/inference/', ingest_inference, name='api_ingest_inference'),
]
