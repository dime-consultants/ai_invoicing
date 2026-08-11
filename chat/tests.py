# ai_invoicing/chat/tests.py
from django.core.files.base import ContentFile
from django.test import TestCase
from rest_framework.test import APIClient
from unittest.mock import patch
from users.models import User
from .models import ChatConversation, ChatMessage


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
            ChatMessage.objects.filter(conversation=existing).order_by('created_at').last().role,
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
