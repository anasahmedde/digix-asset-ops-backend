from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

urlpatterns = [
    path("admin/", admin.site.urls),
    # JWT Auth
    path("api/auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    # API modules
    path("api/accounts/", include("apps.accounts.urls")),
    path("api/assets/", include("apps.assets.urls")),
    path("api/sites/", include("apps.sites.urls")),
    path("api/tickets/", include("apps.tickets.urls")),
    path("api/teams/", include("apps.teams.urls")),
    path("api/warranties/", include("apps.warranties.urls")),
    path("api/maintenance/", include("apps.maintenance.urls")),
    path("api/infrastructure/", include("apps.infrastructure.urls")),
    path("api/inventory/", include("apps.inventory.urls")),
    path("api/suppliers/", include("apps.suppliers.urls")),
    path("api/clients/", include("apps.clients.urls")),
    path("api/procurement/", include("apps.procurement.urls")),
    path("api/finance/", include("apps.finance.urls")),
    path("api/analytics/", include("apps.analytics.urls")),
    # API docs
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    try:
        import debug_toolbar

        urlpatterns = [path("__debug__/", include(debug_toolbar.urls))] + urlpatterns
    except ImportError:
        pass
