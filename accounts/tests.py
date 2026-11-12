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

        self.customer_user = User.objects.create_user(username='regularcustomer', password='Password123!')
        self.customer_profile = UserProfile.objects.create(user=self.customer_user, phone='8888888888', role='customer')

    def test_register_user_success(self):
        data = {
            'username': 'newcustomer',
            'password': 'StrongPassword123!',
            'email': 'customer@example.com',
            'phone': '9876543211',
            'address': 'Chai Street 123'
        }
        res = self.client.post('/api/v1/accounts/register/', data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertIn('tokens', res.data)
        self.assertIn('access', res.data['tokens'])
        self.assertEqual(res.data['user']['username'], 'newcustomer')
        self.assertEqual(res.data['user']['profile']['role'], 'customer')

    def test_registration_ignores_or_rejects_owner_role_escalation(self):
        """Security: public registration must strictly create customer accounts even if role=owner is passed."""
        data = {
            'username': 'hacker',
            'password': 'StrongPassword123!',
            'email': 'hacker@example.com',
            'phone': '9999999999',
            'role': 'owner'  # Malicious attempt to escalate to owner
        }
        res = self.client.post('/api/v1/accounts/register/', data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username='hacker')
        self.assertEqual(user.profile.role, 'customer')

    def test_registration_ignores_or_rejects_delivery_role_escalation(self):
        """Security: public registration must strictly create customer accounts even if role=delivery is passed."""
        data = {
            'username': 'fake_driver',
            'password': 'StrongPassword123!',
            'phone': '9999999998',
            'role': 'delivery'
        }
        res = self.client.post('/api/v1/accounts/register/', data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username='fake_driver')
        self.assertEqual(user.profile.role, 'customer')

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

    def test_profile_update_cannot_escalate_role(self):
        """Security: a customer cannot escalate their role to owner via PATCH profile."""
        self.client.force_authenticate(user=self.customer_user)
        patch_res = self.client.patch('/api/v1/accounts/profile/', {'role': 'owner'}, format='json')
        self.assertEqual(patch_res.status_code, status.HTTP_200_OK)
        self.customer_profile.refresh_from_db()
        self.assertEqual(self.customer_profile.role, 'customer')
    def test_otp_send_and_verify_existing_owner(self):
        """OTP login for existing owner preserves owner role and issues JWT."""
        # 1. Send OTP
        send_res = self.client.post('/api/v1/accounts/otp/send/', {'phone': '9876543210'}, format='json')
        self.assertEqual(send_res.status_code, status.HTTP_200_OK)
        self.assertTrue(send_res.data['success'])
        self.assertTrue(send_res.data['user_exists'])
        session_id = send_res.data['session_id']

        # 2. Verify OTP with valid mock code
        verify_res = self.client.post('/api/v1/accounts/otp/verify/', {
            'phone': '9876543210',
            'session_id': session_id,
            'otp': '123456',
        }, format='json')
        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)
        self.assertIn('tokens', verify_res.data)
        self.assertEqual(verify_res.data['user']['username'], 'testowner')
        self.assertEqual(verify_res.data['user']['profile']['role'], 'owner')

    def test_otp_send_and_verify_new_user_gets_customer_role(self):
        """Public OTP registration creates a customer account and prevents owner escalation."""
        # 1. Send OTP to new mobile
        send_res = self.client.post('/api/v1/accounts/otp/send/', {'phone': '9123456789'}, format='json')
        self.assertEqual(send_res.status_code, status.HTTP_200_OK)
        self.assertFalse(send_res.data['user_exists'])
        session_id = send_res.data['session_id']

        # 2. Verify OTP
        verify_res = self.client.post('/api/v1/accounts/otp/verify/', {
            'phone': '9123456789',
            'session_id': session_id,
            'otp': '123456',
        }, format='json')
        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)
        self.assertIn('tokens', verify_res.data)
        self.assertEqual(verify_res.data['user']['profile']['role'], 'customer')

    def test_otp_verify_invalid_code_fails(self):
        send_res = self.client.post('/api/v1/accounts/otp/send/', {'phone': '9876543210'}, format='json')
        session_id = send_res.data['session_id']

        verify_res = self.client.post('/api/v1/accounts/otp/verify/', {
            'phone': '9876543210',
            'session_id': session_id,
            'otp': '000000',
        }, format='json')
        self.assertEqual(verify_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', verify_res.data)
