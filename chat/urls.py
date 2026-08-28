# chat/urls.py
from django.urls import path

from .views import (
    chat_conversation_list_create,
    chat_conversation_detail,
    chat_message_send,
    chat_message_list,
    ChatInterfaceView,
    workflow_list,
    workflow_defaults,
    workflow_detail,
    chat_attachment_download,
    chat_simple_message,
    chat_history,
    chat_process_file,
    chat_convert_format,
    chat_export_data,
)

urlpatterns = [
    # Web UI
    path("", ChatInterfaceView.as_view(), name="chat_interface"),

    # ── Workflows ──────────────────────────────────────────────────────────────
    # defaults/ must come before <int:pk>/ so it isn't shadowed
    path("workflows/",         workflow_list,     name="workflow-list"),
    path("workflows/defaults/", workflow_defaults, name="workflow-defaults"),
    path("workflows/<int:pk>/", workflow_detail,   name="workflow-detail"),

    # ── Contract endpoints (/api/chat/...) ────────────────────────────────────
    # POST /api/chat/message/          stateless single-turn message
    path("message/",        chat_simple_message,  name="chat-message"),
    # POST /api/chat/process-file/     ingest + extract a file
    path("process-file/",   chat_process_file,    name="chat-process-file"),
    # POST /api/chat/convert-format/   convert file between formats
    path("convert-format/", chat_convert_format,  name="chat-convert-format"),
    # POST /api/chat/export-data/      serialise in-memory data to a file
    path("export-data/",    chat_export_data,     name="chat-export-data"),
    # GET  /api/chat/history           recent conversation messages
    path("history/",        chat_history,         name="chat-history"),

    # ── Conversation-scoped endpoints ─────────────────────────────────────────
    path("conversations/",
         chat_conversation_list_create,
         name="chat_list_create"),
    path("conversations/<int:pk>/",
         chat_conversation_detail,
         name="chat_detail"),
    path("conversations/<int:conversation_id>/messages/",
         chat_message_list,
         name="chat_messages"),
    path("conversations/<int:conversation_id>/send/",
         chat_message_send,
         name="chat_send"),

    # ── Attachment download ───────────────────────────────────────────────────
    path("attachments/<int:attachment_id>/download/",
         chat_attachment_download,
         name="attachment_download"),
]
