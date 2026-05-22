# ai_invoicing/chat/tests.py
from django.test import TestCase
from rest_framework.test import APIClient
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

    def test_send_message(self):
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

    def test_list_conversations(self):
        """Test listing user's conversations."""
        ChatConversation.objects.create(user=self.user, title='Chat 1')
        ChatConversation.objects.create(user=self.user, title='Chat 2')
        
        response = self.client.get('/api/chat/conversations/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 2)
