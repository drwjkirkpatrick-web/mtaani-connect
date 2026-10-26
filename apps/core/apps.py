from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"

    def ready(self):
        # WHY import here: system checks register at startup (Prompt 03).
        from . import checks  # noqa: F401  (import performs the registration)
