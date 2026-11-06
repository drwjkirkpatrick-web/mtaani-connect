from django.apps import AppConfig


class ServicesConfig(AppConfig):
    # WHY: full dotted path 'apps.services' — 'services' alone collides with
    # the global app-registry default namespace and the parent pre-wired this.
    name = "apps.services"
    verbose_name = "Services marketplace"
