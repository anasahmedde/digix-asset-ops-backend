from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.exports import log_export
from common.permissions import can, can_for_project

from .builders import ALIASES, REPORTS, ROW_LIMIT, status_options
from .xlsx import ReportBook

FILTER_KEYS = ("date_from", "date_to", "status", "priority", "category", "project", "site",
               "technician", "group_by", "low_stock")


def _allowed(user, meta) -> bool:
    return can(user, "view_reports") and can(user, meta["capability"])


class ReportCatalogView(APIView):
    """The reports this person may run, grouped as the Reports page shows them."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        reports = [
            {k: v for k, v in meta.items() if k not in ("build", "capability")} | {"statuses": status_options(meta)}
            for meta in REPORTS.values() if _allowed(request.user, meta)
        ]
        return Response({
            "reports": reports,
            # The one-project workbook is reached from here and from the project itself.
            "project_workbook": can(request.user, "view_projects"),
        })


class ReportView(APIView):
    """
    GET /api/reports/generate/?type=<key>[&date_from&date_to&status&...][&export=xlsx]

    JSON for the screen; ``export=xlsx`` for the same report as a formatted workbook.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        key = ALIASES.get(request.query_params.get("type", ""), request.query_params.get("type", ""))
        meta = REPORTS.get(key)
        if meta is None:
            return Response({"detail": f"Unknown report '{key}'.", "available": list(REPORTS)}, status=400)
        if not _allowed(request.user, meta):
            raise PermissionDenied(f"Running the {meta['title']} report is not one of your rights.")
        filters = {k: request.query_params.get(k) for k in FILTER_KEYS if request.query_params.get(k)}
        data = meta["build"](request, filters)
        rows = data["rows"]
        if request.query_params.get("export") == "xlsx":
            log_export(request.user, f"report:{key}", len(rows), filters)
            book = ReportBook(meta["title"], request.user)
            book.table(meta["title"], meta["title"], data["columns"], rows, [_describe(meta, filters)])
            return book.response(key.replace("_", "-") + "-report")
        return Response({
            "type": key, "title": meta["title"], "description": meta["description"],
            "columns": data["columns"],
            "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
            "count": len(rows), "truncated": len(rows) >= ROW_LIMIT,
            "kpis": data.get("kpis", []), "breakdown": data.get("breakdown"),
        })


def _describe(meta, filters) -> str:
    parts = []
    if filters.get("date_from") or filters.get("date_to"):
        parts.append(f"{meta['date_label'] or 'Date'}: {filters.get('date_from', 'start')} to {filters.get('date_to', 'today')}")
    for k in ("status", "priority", "group_by"):
        if filters.get(k):
            parts.append(f"{k.replace('_', ' ').capitalize()}: {filters[k].replace('_', ' ')}")
    if filters.get("low_stock") == "true":
        parts.append("Low stock only")
    return " · ".join(parts) or "All records"


class ProjectWorkbookView(APIView):
    """GET /api/reports/project/<id>/ — the whole project as an Excel workbook."""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from apps.teams.models import Project

        from .project_workbook import build_project_workbook

        project = get_object_or_404(Project.objects.select_related("client", "manager", "site"), pk=pk)
        if not can_for_project(request.user, project, "view_projects"):
            raise PermissionDenied("Seeing this project is not one of your rights.")
        show_costs = can_for_project(request.user, project, "view_project_costs")
        book = build_project_workbook(project, request.user, show_costs)
        log_export(request.user, "report:project", 1, {"project": str(project.pk), "costs": show_costs})
        name = (project.project_code or "project") + "-report"
        return book.response(name)


# Kept for anything still asking for the bare list of report keys.
class ReportTypesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"types": [k for k, m in REPORTS.items() if _allowed(request.user, m)]})
