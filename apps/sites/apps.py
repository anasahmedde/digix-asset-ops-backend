from django.apps import AppConfig


class SitesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sites"
    label = "sites"

    def ready(self):
        import apps.sites.signals  # noqa: F401
