from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from accounts.models import UserProfile
from orders.models import Order
from deliveries.models import Delivery

class DeliveriesTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(username='chaiowner', password='Password123!')
        UserProfile.objects.create(user=self.owner, phone='1111111111', role='owner')

        self.customer = User.objects.create_user(username='customer1', password='Password123!')
        UserProfile.objects.create(user=self.customer, phone='2222222222', role='customer')

        self.driver1 = User.objects.create_user(username='driver1', password='Password123!')
        UserProfile.objects.create(user=self.driver1, phone='3333333333', role='delivery')

        self.driver2 = User.objects.create_user(username='driver2', password='Password123!')
        UserProfile.objects.create(user=self.driver2, phone='4444444444', role='delivery')

        self.order = Order.objects.create(customer=self.customer, delivery_address='Chai Corner', total_amount=40.00)
        self.delivery = Delivery.objects.create(order=self.order, driver=self.driver1, status='assigned')

    def test_driver1_sees_only_assigned_deliveries(self):
        self.client.force_authenticate(user=self.driver1)
        res = self.client.get('/api/v1/deliveries/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)

        # Driver 2 sees 0 deliveries
        self.client.force_authenticate(user=self.driver2)
        res2 = self.client.get('/api/v1/deliveries/')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res2.data), 0)

    def test_driver_can_update_status_and_gps(self):
        self.client.force_authenticate(user=self.driver1)
        update_data = {
            'status': 'picked_up',
            'current_lat': 19.0760,
            'current_lng': 72.8777
        }
        res = self.client.patch(f'/api/v1/deliveries/{self.delivery.id}/', update_data, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'picked_up')
        self.assertIsNotNone(self.delivery.pickup_time)
        self.assertAlmostEqual(self.delivery.current_lat, 19.0760)

    def test_driver_cannot_modify_another_drivers_delivery(self):
        """Security: a driver cannot access or modify another driver's delivery."""
        self.client.force_authenticate(user=self.driver2)
        res = self.client.patch(f'/api/v1/deliveries/{self.delivery.id}/', {'status': 'picked_up'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_driver_cannot_reassign_delivery_to_another_driver(self):
        """Security: an assigned driver cannot reassign the driver foreign key."""
        self.client.force_authenticate(user=self.driver1)
        res = self.client.patch(f'/api/v1/deliveries/{self.delivery.id}/', {'driver': self.driver2.id}, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.driver, self.driver1)

    def test_customer_cannot_update_delivery_status(self):
        """Security: customer can only read delivery info for their order, not modify it."""
        self.client.force_authenticate(user=self.customer)
        # GET is permitted (view status of their tea delivery)
        res_get = self.client.get(f'/api/v1/deliveries/{self.delivery.id}/')
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)

        # PATCH is forbidden
        res_patch = self.client.patch(f'/api/v1/deliveries/{self.delivery.id}/', {'status': 'delivered'}, format='json')
        self.assertEqual(res_patch.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_reassign_delivery(self):
        self.client.force_authenticate(user=self.owner)
        res = self.client.patch(f'/api/v1/deliveries/{self.delivery.id}/', {'driver': self.driver2.id}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.driver, self.driver2)
