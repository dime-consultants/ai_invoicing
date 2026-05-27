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
    ChatSimpleMessageView,
    ChatHistoryView,
    ChatProcessFileView,
    ChatConvertFormatView,
    ChatExportDataView,
)

router = DefaultRouter()
router.register(r'workflows', WorkflowViewSet, basename='workflow')

urlpatterns = [
    # Web UI
    path("", ChatInterfaceView.as_view(), name="chat_interface"),

    # Router for workflows
    path("", include(router.urls)),

    # ── Contract endpoints (/api/chat/...) ────────────────────────────────────
    # POST /api/chat/message/          stateless single-turn message
    path("message/",        ChatSimpleMessageView.as_view(),  name="chat-message"),
    # POST /api/chat/process-file/     process uploaded file → structured data
    path("process-file/",   ChatProcessFileView.as_view(),    name="chat-process-file"),
    # POST /api/chat/convert-format/   convert file between formats
    path("convert-format/", ChatConvertFormatView.as_view(),  name="chat-convert-format"),
    # POST /api/chat/export-data/      export in-memory data to file
    path("export-data/",    ChatExportDataView.as_view(),     name="chat-export-data"),
    # GET  /api/chat/history           chat history
    path("history/",        ChatHistoryView.as_view(),        name="chat-history"),

    # ── Conversation-based endpoints ──────────────────────────────────────────
    path("conversations/",                                ChatConversationListCreateView.as_view(), name="chat_list_create"),
    path("conversations/<int:pk>/",                       ChatConversationDetailView.as_view(),     name="chat_detail"),
    path("conversations/<int:conversation_id>/messages/", ChatMessageListView.as_view(),            name="chat_messages"),
    path("conversations/<int:conversation_id>/send/",     ChatMessageSendView.as_view(),            name="chat_send"),

    # File download
    path("attachments/<int:attachment_id>/download/",     ChatAttachmentDownloadView.as_view(),     name="attachment_download"),
]