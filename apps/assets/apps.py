from django.apps import AppConfig


class AssetsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.assets"
    label = "assets"

    def ready(self):
        import apps.assets.signals  # noqa: F401
