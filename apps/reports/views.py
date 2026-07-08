from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .builders import BUILDERS


class ReportView(APIView):
    """
    Flexible report endpoint.

    GET /api/reports/generate/?type=<type>&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD[&status=...]

    Types: assets, tickets, work_orders, inventory, suppliers, clients, teams.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        report_type = request.query_params.get("type", "assets")
        builder = BUILDERS.get(report_type)
        if builder is None:
            return Response(
                {"detail": f"Unknown report type '{report_type}'.", "available": list(BUILDERS)},
                status=400,
            )
        date_from = request.query_params.get("date_from") or None
        date_to = request.query_params.get("date_to") or None
        return Response(builder(request, date_from, date_to))


class ReportTypesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"types": list(BUILDERS)})
