from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from accounts.models import UserProfile
from notifications.models import DeviceToken
from notifications.services import NotificationService

class NotificationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='notifyuser', password='Password123!')
        self.profile = UserProfile.objects.create(user=self.user, phone='9876500000', role='customer')

        self.user2 = User.objects.create_user(username='otheruser', password='Password123!')
        UserProfile.objects.create(user=self.user2, phone='9876500001', role='customer')

    def test_register_device_token(self):
        self.client.force_authenticate(user=self.user)
        data = {
            'token': 'fcm_test_token_12345',
            'platform': 'android',
        }
        res = self.client.post('/api/v1/notifications/devices/', data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(DeviceToken.objects.filter(user=self.user).count(), 1)
        token_obj = DeviceToken.objects.get(token='fcm_test_token_12345')
        self.assertTrue(token_obj.is_active)
        self.assertEqual(token_obj.user, self.user)

    def test_user_cannot_access_or_modify_other_user_tokens(self):
        # User 1 registers token
        token1 = DeviceToken.objects.create(user=self.user, token='token_user1')

        # User 2 logs in
        self.client.force_authenticate(user=self.user2)
        res = self.client.get('/api/v1/notifications/devices/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 0)

        # User 2 attempts to delete user 1's token
        del_res = self.client.delete(f'/api/v1/notifications/devices/{token1.id}/')
        self.assertEqual(del_res.status_code, status.HTTP_404_NOT_FOUND)

    def test_deactivate_token_on_logout(self):
        self.client.force_authenticate(user=self.user)
        token1 = DeviceToken.objects.create(user=self.user, token='token_to_logout', is_active=True)

        res = self.client.post('/api/v1/notifications/devices/deactivate/', {
            'token': 'token_to_logout'
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        token1.refresh_from_db()
        self.assertFalse(token1.is_active)

    def test_reusable_service_send_multicast_mock_mode(self):
        """NotificationService executes safely in mock/log mode without raising exceptions."""
        token1 = DeviceToken.objects.create(user=self.user, token='test_mock_token')
        # Should execute safely without error
        NotificationService.send_to_user(
            user=self.user,
            title="Test Push",
            body="Your tea is ready",
            data={'order_id': 1}
        )
