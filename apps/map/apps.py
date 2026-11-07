"""App config for the Map app (Prompt 28).

WHY the name must be "apps.map": the parent already wired
``"apps.map"`` into INSTALLED_APPS and ``path("map/", include("apps.map.urls"))``
into mtaani/urls.py — this config resolves that module (crowd/tracking pattern).

NO MODELS by design: the map OWNS no transport data. Positions come through
Prompt 27's public doors (services.visible_positions / live_map_payload /
session_positions shapes), crowd overlays through Prompt 30's
crowd_overlay_for_stage — this app is a VIEWER over other apps' truth, never
a second source of it (KDPA: one path to every position).
"""
from django.apps import AppConfig


class MapConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.map"
    verbose_name = "Live map"
