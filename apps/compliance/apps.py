"""App config for compliance (Prompt 40 — the owning subagent completes it).

Loaded last-ish in INSTALLED_APPS (parent-wired): the app only READS
siblings through their public doors and never registers signals — so
ready() stays empty on purpose (no cross-app import-time coupling)."""
from django.apps import AppConfig

class ComplianceConfig(AppConfig):
    name = "apps.compliance"
    verbose_name = "KDPA compliance"
