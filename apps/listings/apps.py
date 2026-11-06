from django.apps import AppConfig

# NOTE: listings app (Prompt 17 — marketplace goods). Registered in INSTALLED_APPS by parent.
class ListingsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.listings"
    verbose_name = "Listings"
