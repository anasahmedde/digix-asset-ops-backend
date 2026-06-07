from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ChatMessageViewSet, ChatRoomViewSet

router = DefaultRouter()
router.register("rooms", ChatRoomViewSet, basename="chatroom")
router.register("messages", ChatMessageViewSet, basename="chatmessage")

urlpatterns = [
    path("", include(router.urls)),
]
