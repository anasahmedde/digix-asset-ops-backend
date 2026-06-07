from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import AdminManagerWriteElseRead

from .models import Document
from .serializers import DocumentSerializer


class DocumentViewSet(viewsets.ModelViewSet):
    queryset = Document.objects.select_related(
        "device", "site", "project", "uploaded_by"
    ).all()
    serializer_class = DocumentSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["doc_type", "device", "site", "project", "installation", "uploaded_by"]
    search_fields = ["title", "description"]
    ordering_fields = ["created_at", "title"]
