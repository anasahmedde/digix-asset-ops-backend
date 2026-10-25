from django.urls import path

from .views import ProjectWorkbookView, ReportCatalogView, ReportTypesView, ReportView

urlpatterns = [
    path("catalog/", ReportCatalogView.as_view(), name="report-catalog"),
    path("generate/", ReportView.as_view(), name="report-generate"),
    path("project/<uuid:pk>/", ProjectWorkbookView.as_view(), name="report-project"),
    path("types/", ReportTypesView.as_view(), name="report-types"),
]
