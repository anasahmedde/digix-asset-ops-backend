"""`/api/health/` for the load balancer in front of the app.

Answered before any other middleware: the load balancer calls it by the
container's own address, which the allowed-hosts check would refuse with 400
and so take a healthy app out of service. 200 when the database answers.
"""

from django.db import connection
from django.http import HttpResponse

PATH = "/api/health/"


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path != PATH:
            return self.get_response(request)
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception:
            return HttpResponse("database unavailable", status=503, content_type="text/plain")
        return HttpResponse("ok", content_type="text/plain")
