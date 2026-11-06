from django.apps import AppConfig

# WHY django.apps.AppConfig subclass: default_app_config is dead since 3.2 —
# the explicit AppConfig with name='apps.feed' is what makes this package a
# proper app even though the directory lives under apps/.


class FeedConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.feed"
    verbose_name = "Utility feed"
