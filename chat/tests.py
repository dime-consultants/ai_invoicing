# ai_invoicing/chat/tests.py
from types import SimpleNamespace

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from unittest.mock import patch
from users.models import User
from .models import ChatConversation, ChatMessage, ChatMessageAttachment, Workflow
from .services import ChatService


class _ImmediateAsyncResult:
    """
    Stand-in for a Celery AsyncResult that already has its value, so tests
    exercise chat/views.py's real dispatch-and-wait logic
    (_run_turn_via_worker) without needing a real broker/worker — see
    config/dispatch.py:dispatch(), which the view calls and this replaces.
    """

    def __init__(self, value=None, exc=None):
        self._value = value
        self._exc = exc

    def get(self, timeout=None):
        if self._exc:
            raise self._exc
        return self._value


def _dispatch_creating_assistant_reply(content: str, attachments: list | None = None):
    """
    side_effect for a mocked config.dispatch.dispatch(): creates the
    assistant ChatMessage (and any output ChatMessageAttachment rows) that
    the real run_chat_turn_task would have created and saved, then returns
    an _ImmediateAsyncResult pointing at them — matching the
    {"assistant_message_id", "attachment_ids"} contract
    _run_turn_via_worker expects back from AsyncResult.get().
    """

    def _dispatch(task, **kwargs):
        conv = ChatConversation.objects.get(pk=kwargs["conversation_id"])
        assistant_msg = ChatMessage.objects.create(
            conversation=conv, role="assistant", content=content,
        )
        attachment_ids = []
        for filename in (attachments or []):
            att = ChatMessageAttachment(
                message=assistant_msg, filename=filename,
                file_type=filename.rsplit(".", 1)[-1],
                attachment_type="assistant_output",
            )
            att.file.save(filename, ContentFile(b"abc"), save=True)
            attachment_ids.append(att.pk)
        return _ImmediateAsyncResult(value={
            "ok": True,
            "assistant_message_id": assistant_msg.pk,
            "attachment_ids": attachment_ids,
        })

    return _dispatch


class ChatInterfaceTestCase(TestCase):
    
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.client.force_authenticate(user=self.user)

    def test_create_conversation(self):
        """Test creating a new chat conversation."""
        response = self.client.post(
            '/api/chat/conversations/',
            {'title': 'Test Conversation'},
            format='json'
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['title'], 'Test Conversation')

    @patch('config.dispatch.dispatch')
    def test_send_message(self, mock_dispatch):
        """Test sending a message and getting a response."""
        mock_dispatch.side_effect = _dispatch_creating_assistant_reply('Hello from AI')
        conversation = ChatConversation.objects.create(
            user=self.user,
            title='Test Chat'
        )

        response = self.client.post(
            f'/api/chat/conversations/{conversation.pk}/send/',
            {'message': 'help'},
            format='json'
        )
        self.assertEqual(response.status_code, 201)
        self.assertIn('user_message', response.data)
        self.assertIn('assistant_message', response.data)
        self.assertEqual(response.data['assistant_message']['content'], 'Hello from AI')

    @patch('config.dispatch.dispatch')
    def test_send_message_returns_download_url_for_output_attachment(self, mock_dispatch):
        """Output attachments should include a downloadable URL."""
        mock_dispatch.side_effect = _dispatch_creating_assistant_reply(
            'Assistant reply', attachments=['report.xlsx'],
        )
        conversation = ChatConversation.objects.create(
            user=self.user,
            title='Test Chat'
        )

        response = self.client.post(
            f'/api/chat/conversations/{conversation.pk}/send/',
            {'message': 'create report'},
            format='json'
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data['output_attachments'])
        self.assertIn('download_url', response.data['output_attachments'][0])
        self.assertIn('/api/chat/attachments/', response.data['output_attachments'][0]['download_url'])

    @override_settings(XAI_API_KEY='test-key')
    @patch('chat.services.ChatService._run_agent', return_value=('Fast reply', None))
    def test_no_attachment_messages_skip_bulk_file_processing(self, mock_run_agent):
        """No-file messages should avoid upload batch setup and use the fast path."""
        response_text, output_files = ChatService.get_response(
            'Say hello briefly.',
            self.user,
            file_attachments=[],
        )

        self.assertEqual(response_text, 'Fast reply')
        self.assertEqual(output_files, [])
        self.assertIsNone(mock_run_agent.call_args.kwargs['batch'])
        self.assertIsNone(mock_run_agent.call_args.kwargs['workflow'])

    def test_workflow_inference_handles_request_and_file_context(self):
        """Workflow selection should reflect user intent when files are present."""
        # Workflow rows are normally seeded by `manage.py init_workflows`, which
        # doesn't run for tests — without a matching enabled row, resolution
        # correctly returns None regardless of the keyword match, so this test
        # needs its own fixture to actually exercise the reconciliation branch.
        Workflow.objects.create(
            name="Reconciliation", workflow_type="reconciliation", enabled=True,
        )
        workflow = ChatService._resolve_workflow_for_message(
            'Reconcile these files and generate the variance report.',
            [
                SimpleNamespace(file_type='pdf'),
                SimpleNamespace(file_type='xlsx'),
            ],
            None,
        )
        self.assertIsNotNone(workflow)
        self.assertEqual(workflow.workflow_type, 'reconciliation')

    @patch('config.dispatch.dispatch')
    def test_create_new_chat_and_continue_existing_chat(self, mock_dispatch):
        """Test creating a new conversation and continuing an existing chat."""
        mock_dispatch.side_effect = _dispatch_creating_assistant_reply('Assistant reply')
        existing = ChatConversation.objects.create(user=self.user, title='Existing Chat')
        ChatMessage.objects.create(conversation=existing, role='user', content='First message')

        create_response = self.client.post(
            '/api/chat/conversations/',
            {'title': 'New Chat'},
            format='json'
        )
        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.data['title'], 'New Chat')

        continue_response = self.client.post(
            f'/api/chat/conversations/{existing.pk}/send/',
            {'message': 'Continue this chat'},
            format='json'
        )
        self.assertEqual(continue_response.status_code, 201)
        self.assertEqual(continue_response.data['assistant_message']['content'], 'Assistant reply')

        self.assertEqual(
            ChatMessage.objects.filter(conversation=existing).count(),
            3
        )
        self.assertEqual(
            ChatMessage.objects.filter(conversation=existing).order_by('created_at', 'pk').last().role,
            'assistant'
        )

    def test_list_conversations(self):
        """Test listing user's conversations."""
        ChatConversation.objects.create(user=self.user, title='Chat 1')
        ChatConversation.objects.create(user=self.user, title='Chat 2')
        
        response = self.client.get('/api/chat/conversations/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)

    def test_chat_history_returns_correct_messages(self):
        """Test viewing chat history for specific conversations and most recent chat."""
        conv_old = ChatConversation.objects.create(user=self.user, title='Old Chat')
        ChatMessage.objects.create(conversation=conv_old, role='user', content='old message')

        conv_new = ChatConversation.objects.create(user=self.user, title='New Chat')
        ChatMessage.objects.create(conversation=conv_new, role='user', content='new message')

        response = self.client.get('/api/chat/history/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['messages']), 1)
        self.assertEqual(response.data['messages'][0]['content'], 'new message')

        response_by_id = self.client.get('/api/chat/history/', {'conversationId': conv_old.pk})
        self.assertEqual(response_by_id.status_code, 200)
        self.assertEqual(len(response_by_id.data['messages']), 1)
        self.assertEqual(response_by_id.data['messages'][0]['content'], 'old message')
