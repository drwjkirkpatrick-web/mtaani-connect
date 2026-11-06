"""Moderation app: reports, blocks, queue, audit.

NOTE: Created per Prompt 15. Role gates live here reading accounts PUBLIC
attributes only (is_staff/is_superuser); a dedicated moderator table arrives
with Prompt 37.
WHY: removals are represented by ContentStatus.REMOVED on the target object,
never hard deletion (evidence retention for disputes).
"""

from django.apps import AppConfig


class ModerationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.moderation"
    verbose_name = "Moderation"
