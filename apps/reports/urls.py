from django.urls import path

from .views import ReportTypesView, ReportView

urlpatterns = [
    path("generate/", ReportView.as_view(), name="report-generate"),
    path("types/", ReportTypesView.as_view(), name="report-types"),
]
