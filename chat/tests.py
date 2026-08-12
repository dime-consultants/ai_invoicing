# ai_invoicing/chat/tests.py
from types import SimpleNamespace

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from unittest.mock import patch
from users.models import User
from .models import ChatConversation, ChatMessage, Workflow
from .services import ChatService


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

    @patch('chat.views.ChatService.get_response', return_value=('Hello from AI', []))
    def test_send_message(self, mock_get_response):
        """Test sending a message and getting a response."""
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

    @patch('chat.views.ChatService.get_response', return_value=('Assistant reply', [
        {'filename': 'report.xlsx', 'content': ContentFile(b'abc', name='report.xlsx')}
    ]))
    def test_send_message_returns_download_url_for_output_attachment(self, mock_get_response):
        """Output attachments should include a downloadable URL."""
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

    @patch('chat.views.ChatService.get_response', return_value=('Assistant reply', []))
    def test_create_new_chat_and_continue_existing_chat(self, mock_get_response):
        """Test creating a new conversation and continuing an existing chat."""
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
