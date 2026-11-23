from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from accounts.models import UserProfile
from notifications.models import DeviceToken, NotificationLog
from notifications.services import NotificationService

class NotificationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(username='owneruser', password='Password123!', is_staff=True)
        self.owner_profile = UserProfile.objects.create(user=self.owner, phone='9876543210', role='owner')

        self.driver = User.objects.create_user(username='driveruser', password='Password123!')
        self.driver_profile = UserProfile.objects.create(user=self.driver, phone='9555444333', role='delivery')

        self.customer = User.objects.create_user(username='custuser', password='Password123!')
        self.cust_profile = UserProfile.objects.create(user=self.customer, phone='9111222333', role='customer')

    def test_register_device_token(self):
        self.client.force_authenticate(user=self.driver)
        data = {
            'token': 'fcm_test_token_12345',
            'platform': 'android',
        }
        res = self.client.post('/api/v1/notifications/devices/', data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(DeviceToken.objects.filter(user=self.driver).count(), 1)
        token_obj = DeviceToken.objects.get(token='fcm_test_token_12345')
        self.assertTrue(token_obj.is_active)
        self.assertEqual(token_obj.user, self.driver)

    def test_user_cannot_access_or_modify_other_user_tokens(self):
        token1 = DeviceToken.objects.create(user=self.owner, token='token_owner')

        self.client.force_authenticate(user=self.driver)
        res = self.client.get('/api/v1/notifications/devices/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 0)

        del_res = self.client.delete(f'/api/v1/notifications/devices/{token1.id}/')
        self.assertEqual(del_res.status_code, status.HTTP_404_NOT_FOUND)

    def test_deactivate_token_on_logout(self):
        self.client.force_authenticate(user=self.driver)
        token1 = DeviceToken.objects.create(user=self.driver, token='token_to_logout', is_active=True)

        res = self.client.post('/api/v1/notifications/devices/deactivate/', {
            'token': 'token_to_logout'
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        token1.refresh_from_db()
        self.assertFalse(token1.is_active)

    def test_fcm_notification_logging_and_history(self):
        """NotificationService records FCM delivery history with target role."""
        NotificationService.send_to_user(
            user=self.driver,
            title="Trip Assigned",
            body="Deliver order #101 to Tech Park",
            data={'order_id': 101}
        )
        self.assertEqual(NotificationLog.objects.filter(recipient=self.driver, channel='fcm').count(), 1)
        log = NotificationLog.objects.filter(recipient=self.driver).first()
        self.assertEqual(log.status, 'sent')
        self.assertEqual(log.target_role, 'delivery')

    def test_sms_notification_logging_and_mock_dispatch(self):
        """NotificationService dispatches SMS notifications via backend provider and logs delivery."""
        res = NotificationService.send_sms(
            phone='9555444333',
            message='ChaiFlow: You have an urgent delivery assignment.',
            recipient=self.driver,
            target_role='delivery',
        )
        self.assertTrue(res)
        self.assertEqual(NotificationLog.objects.filter(recipient=self.driver, channel='sms').count(), 1)
        sms_log = NotificationLog.objects.filter(recipient=self.driver, channel='sms').first()
        self.assertEqual(sms_log.status, 'sent')
        self.assertIsNotNone(sms_log.sent_at)

    def test_role_based_notification_history_access(self):
        """Owner sees owner notifications; Delivery partner sees delivery notifications."""
        # 1. Create logs
        NotificationLog.objects.create(
            recipient=self.owner,
            target_role='owner',
            channel='fcm',
            title='Owner Daily Summary',
            body='Revenue ₹12,500'
        )
        NotificationLog.objects.create(
            recipient=self.driver,
            target_role='delivery',
            channel='sms',
            title='SMS Trip Alert',
            body='Trip assigned'
        )

        # 2. Authenticate as Delivery Partner
        self.client.force_authenticate(user=self.driver)
        driver_res = self.client.get('/api/v1/notifications/history/')
        self.assertEqual(driver_res.status_code, status.HTTP_200_OK)
        driver_titles = [item['title'] for item in driver_res.data]
        self.assertIn('SMS Trip Alert', driver_titles)
        self.assertNotIn('Owner Daily Summary', driver_titles)

        # 3. Authenticate as Owner
        self.client.force_authenticate(user=self.owner)
        owner_res = self.client.get('/api/v1/notifications/history/')
        self.assertEqual(owner_res.status_code, status.HTTP_200_OK)
        owner_titles = [item['title'] for item in owner_res.data]
        self.assertIn('Owner Daily Summary', owner_titles)

    def test_retry_failed_notifications(self):
        """Failed notifications can be retried without exceeding max retries."""
        failed_log = NotificationLog.objects.create(
            recipient=self.driver,
            target_role='delivery',
            channel='sms',
            title='Retry Alert',
            body='Important trip reminder',
            data_payload={'phone': '9555444333'},
            status='failed',
            retry_count=0,
            max_retries=3,
        )

        self.client.force_authenticate(user=self.owner)
        retry_res = self.client.post('/api/v1/notifications/history/retry_failed/')
        self.assertEqual(retry_res.status_code, status.HTTP_200_OK)
        self.assertEqual(retry_res.data['retried_count'], 1)

        failed_log.refresh_from_db()
        self.assertEqual(failed_log.retry_count, 1)
        self.assertEqual(failed_log.status, 'sent')

    def test_authenticated_user_can_delete_own_notification(self):
        """User can delete their own notification record via DELETE /api/v1/notifications/history/{id}/."""
        log = NotificationLog.objects.create(
            recipient=self.driver,
            target_role='delivery',
            channel='fcm',
            title='Driver Notification',
            body='Trip assigned'
        )
        self.client.force_authenticate(user=self.driver)
        res = self.client.delete(f'/api/v1/notifications/history/{log.id}/')
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(NotificationLog.objects.filter(id=log.id).exists())

    def test_user_cannot_delete_other_user_notification(self):
        """User cannot delete another user's private notification."""
        owner_log = NotificationLog.objects.create(
            recipient=self.owner,
            target_role='owner',
            channel='fcm',
            title='Owner Secret',
            body='Confidential owner alert'
        )
        self.client.force_authenticate(user=self.driver)
        res = self.client.delete(f'/api/v1/notifications/history/{owner_log.id}/')
        # Filtered queryset means 404 Not Found for other users
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(NotificationLog.objects.filter(id=owner_log.id).exists())

