from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import AdminManagerWriteElseRead

from .models import Document
from .serializers import DocumentSerializer


class DocumentViewSet(viewsets.ModelViewSet):
    queryset = Document.objects.select_related(
        "device", "site", "project", "ticket", "work_order", "uploaded_by"
    ).all()
    serializer_class = DocumentSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = [
        "doc_type", "device", "site", "project", "installation",
        "ticket", "work_order", "uploaded_by",
    ]
    search_fields = ["title", "description"]
    ordering_fields = ["created_at", "title"]

    def perform_create(self, serializer):
        upload = self.request.FILES.get("file")
        serializer.save(
            uploaded_by=self.request.user,
            file_size=upload.size if upload else 0,
        )
