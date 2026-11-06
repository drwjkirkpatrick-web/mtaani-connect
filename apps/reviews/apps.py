from django.apps import AppConfig

# WHY app config: parent pre-wired 'apps.reviews' in INSTALLED_APPS; explicit name keeps
# module-path resolution stable under both `manage.py` and pytest discovery.
class ReviewsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reviews"
    verbose_name = "Reviews"
