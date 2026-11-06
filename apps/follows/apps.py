from django.apps import AppConfig


class FollowsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.follows"
    verbose_name = "Follows"  # gettext? kept simple: app label, marked where user-visible.
