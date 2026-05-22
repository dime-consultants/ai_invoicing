# ai_invoicing/chat/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ChatConversationListCreateView,
    ChatConversationDetailView,
    ChatMessageSendView,
    ChatMessageListView,
    ChatInterfaceView,
    WorkflowViewSet,
    ChatAttachmentDownloadView,
)

router = DefaultRouter()
router.register(r'workflows', WorkflowViewSet, basename='workflow')

urlpatterns = [
    # Web UI
    path("", ChatInterfaceView.as_view(), name="chat_interface"),
    
    # Router for workflows
    path("", include(router.urls)),
    
    # API
    path("conversations/", ChatConversationListCreateView.as_view(), name="chat_list_create"),
    path("conversations/<int:pk>/", ChatConversationDetailView.as_view(), name="chat_detail"),
    path("conversations/<int:conversation_id>/messages/", ChatMessageListView.as_view(), name="chat_messages"),
    path("conversations/<int:conversation_id>/send/", ChatMessageSendView.as_view(), name="chat_send"),
    
    # File management
    path("attachments/<int:attachment_id>/download/", ChatAttachmentDownloadView.as_view(), name="attachment_download"),
]
