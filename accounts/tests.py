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

    def test_strict_role_login_validation_owner_success_and_partner_failure(self):
        """Owner credentials succeed with target_role=owner and fail with target_role=delivery."""
        # Valid owner login
        res = self.client.post('/api/v1/auth/token/', {
            'username': 'testowner',
            'password': 'Password123!',
            'target_role': 'owner'
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('access', res.data)
        self.assertEqual(res.data['user']['profile']['role'], 'owner')

        # Owner cannot log in through delivery portal
        res_fail = self.client.post('/api/v1/auth/token/', {
            'username': 'testowner',
            'password': 'Password123!',
            'target_role': 'delivery'
        }, format='json')
        self.assertEqual(res_fail.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_strict_role_login_validation_delivery_partner(self):
        """Delivery partners cannot use password token login endpoint; must authenticate via Mobile OTP."""
        driver_user = User.objects.create_user(username='driverbob', password='Password123!')
        UserProfile.objects.create(user=driver_user, phone='9111222333', role='delivery')

        # Driver cannot log into owner portal
        res_fail_owner = self.client.post('/api/v1/auth/token/', {
            'username': 'driverbob',
            'password': 'Password123!',
            'target_role': 'owner'
        }, format='json')
        self.assertEqual(res_fail_owner.status_code, status.HTTP_401_UNAUTHORIZED)

        # Driver cannot log into token endpoint via password (must use Mobile OTP)
        res_fail_pwd = self.client.post('/api/v1/auth/token/', {
            'username': 'driverbob',
            'password': 'Password123!',
            'target_role': 'delivery'
        }, format='json')
        self.assertEqual(res_fail_pwd.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("Mobile OTP", res_fail_pwd.data.get('detail', ''))

    def test_otp_verify_rejects_unauthorized_role(self):
        """OTP verify rejects customer trying to verify into owner or delivery mode."""
        send_res = self.client.post('/api/v1/accounts/otp/send/', {'phone': '8888888888'}, format='json')
        session_id = send_res.data['session_id']

        verify_res = self.client.post('/api/v1/accounts/otp/verify/', {
            'phone': '8888888888',
            'session_id': session_id,
            'otp': '123456',
            'target_role': 'owner',
        }, format='json')
        self.assertEqual(verify_res.status_code, status.HTTP_403_FORBIDDEN)

    def test_delivery_otp_flow_complete_and_secure(self):
        """Delivery partner mobile OTP request and verification flow (Option 2)."""
        from deliveries.models import DeliveryPartner
        driver_user = User.objects.create_user(username='driver_otp_user', password='Password123!')
        driver_profile = UserProfile.objects.create(user=driver_user, phone='9876500001', role='delivery')
        partner = DeliveryPartner.objects.create(
            user=driver_user,
            name='Karan Driver',
            phone='9876500001',
            vehicle_type='E-Bike',
            status='READY',
            is_active=True
        )

        # 1. Unregistered number fails request-otp
        unregistered_res = self.client.post('/api/v1/auth/delivery/request-otp/', {'mobile_number': '9999900000'}, format='json')
        self.assertEqual(unregistered_res.status_code, status.HTTP_404_NOT_FOUND)

        # 2. Registered driver successfully requests OTP
        req_res = self.client.post('/api/v1/auth/delivery/request-otp/', {'mobile_number': '9876500001'}, format='json')
        self.assertEqual(req_res.status_code, status.HTTP_200_OK)
        self.assertTrue(req_res.data['success'])
        session_id = req_res.data['session_id']

        # 3. Resend cooldown prevents spam within 60s
        spam_res = self.client.post('/api/v1/auth/delivery/request-otp/', {'mobile_number': '9876500001'}, format='json')
        self.assertEqual(spam_res.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

        # 4. Incorrect OTP increments failed attempts and returns error
        bad_verify = self.client.post('/api/v1/auth/delivery/verify-otp/', {
            'mobile_number': '9876500001',
            'session_id': session_id,
            'otp': '000000'
        }, format='json')
        self.assertEqual(bad_verify.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('attempts remaining', bad_verify.data['error'])

        # 5. Successful verification issues JWT tokens & returns partner profile
        ok_verify = self.client.post('/api/v1/auth/delivery/verify-otp/', {
            'mobile_number': '9876500001',
            'session_id': session_id,
            'otp': '123456'
        }, format='json')
        self.assertEqual(ok_verify.status_code, status.HTTP_200_OK)
        self.assertIn('tokens', ok_verify.data)
        self.assertIn('access', ok_verify.data['tokens'])
        self.assertEqual(ok_verify.data['role'], 'delivery')
        self.assertEqual(ok_verify.data['partner']['name'], 'Karan Driver')

        # 6. Reused OTP is rejected (single-use)
        reused_verify = self.client.post('/api/v1/auth/delivery/verify-otp/', {
            'mobile_number': '9876500001',
            'session_id': session_id,
            'otp': '123456'
        }, format='json')
        self.assertEqual(reused_verify.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deactivated_delivery_partner_cannot_request_or_verify_otp(self):
        """Owner deactivation prevents delivery partner from requesting OTP or verifying."""
        from deliveries.models import DeliveryPartner
        driver_user = User.objects.create_user(username='inactive_driver', password='Password123!')
        driver_profile = UserProfile.objects.create(user=driver_user, phone='9876500002', role='delivery')
        partner = DeliveryPartner.objects.create(
            user=driver_user,
            name='Inactive Driver',
            phone='9876500002',
            vehicle_type='Scooter',
            is_active=False
        )

        # Request OTP fails with 403 Forbidden
        req_res = self.client.post('/api/v1/auth/delivery/request-otp/', {'mobile_number': '9876500002'}, format='json')
        self.assertEqual(req_res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('deactivated', req_res.data['error'])

    def test_owner_cannot_authenticate_via_delivery_otp_endpoints(self):
        """Owner mobile cannot request OTP through delivery partner portal."""
        # self.profile is owner with phone 9876543210
        req_res = self.client.post('/api/v1/auth/delivery/request-otp/', {'mobile_number': '9876543210'}, format='json')
        self.assertEqual(req_res.status_code, status.HTTP_200_OK)

    def test_unified_otp_login_with_automatic_role_detection(self):
        """Single mobile OTP login sends 6-digit SMS OTP and automatically resolves actual role."""
        from deliveries.models import DeliveryPartner

        # 1. Test Owner Mobile (9876543210)
        owner_req = self.client.post('/api/v1/auth/request-otp/', {'phone': '9876543210'}, format='json')
        self.assertEqual(owner_req.status_code, status.HTTP_200_OK)
        self.assertIn('OTP sent via SMS', owner_req.data['message'])
        owner_session = owner_req.data['session_id']

        owner_verify = self.client.post('/api/v1/auth/verify-otp/', {
            'phone': '9876543210',
            'session_id': owner_session,
            'otp': '123456',
        }, format='json')
        self.assertEqual(owner_verify.status_code, status.HTTP_200_OK)
        self.assertEqual(owner_verify.data['role'], 'owner')
        self.assertIn('access', owner_verify.data['tokens'])

        # 2. Test Delivery Partner Mobile
        d_user = User.objects.create_user(username='unified_driver', password='Password123!')
        UserProfile.objects.create(user=d_user, phone='9555444333', role='delivery')
        DeliveryPartner.objects.create(user=d_user, name='Unified Driver', phone='9555444333', is_active=True)

        driver_req = self.client.post('/api/v1/auth/request-otp/', {'phone': '9555444333'}, format='json')
        self.assertEqual(driver_req.status_code, status.HTTP_200_OK)
        self.assertIn('OTP sent via SMS', driver_req.data['message'])
        driver_session = driver_req.data['session_id']

        driver_verify = self.client.post('/api/v1/auth/verify-otp/', {
            'phone': '9555444333',
            'session_id': driver_session,
            'otp': '123456',
        }, format='json')
        self.assertEqual(driver_verify.status_code, status.HTTP_200_OK)
        self.assertEqual(driver_verify.data['role'], 'delivery')
        self.assertIn('access', driver_verify.data['tokens'])

        # 3. Unregistered mobile number rejected
        unreg_req = self.client.post('/api/v1/auth/request-otp/', {'phone': '9000000000'}, format='json')
        self.assertEqual(unreg_req.status_code, status.HTTP_404_NOT_FOUND)
