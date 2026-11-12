from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from accounts.models import UserProfile

class AccountsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='testowner', password='Password123!', email='owner@example.com')
        self.profile = UserProfile.objects.create(user=self.user, phone='9876543210', role='owner')

    def test_register_user_success(self):
        data = {
            'username': 'newcustomer',
            'password': 'StrongPassword123!',
            'email': 'customer@example.com',
            'phone': '9876543211',
            'role': 'customer',
            'address': 'Chai Street 123'
        }
        res = self.client.post('/api/v1/accounts/register/', data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertIn('tokens', res.data)
        self.assertIn('access', res.data['tokens'])
        self.assertEqual(res.data['user']['username'], 'newcustomer')
        self.assertEqual(res.data['user']['profile']['role'], 'customer')

    def test_register_duplicate_phone_fails(self):
        data = {
            'username': 'anotheruser',
            'password': 'StrongPassword123!',
            'phone': '9876543210'  # already exists
        }
        res = self.client.post('/api/v1/accounts/register/', data, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_jwt_token(self):
        res = self.client.post('/api/v1/auth/token/', {'username': 'testowner', 'password': 'Password123!'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('access', res.data)
        self.assertIn('refresh', res.data)

    def test_profile_retrieval_and_update(self):
        # Authenticate
        self.client.force_authenticate(user=self.user)
        res = self.client.get('/api/v1/accounts/profile/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['username'], 'testowner')
        self.assertEqual(res.data['profile']['phone'], '9876543210')

        # Update profile
        update_data = {'address': 'New Updated Address', 'first_name': 'OwnerFirstName'}
        patch_res = self.client.patch('/api/v1/accounts/profile/', update_data, format='json')
        self.assertEqual(patch_res.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_res.data['first_name'], 'OwnerFirstName')
        self.assertEqual(patch_res.data['profile']['address'], 'New Updated Address')
