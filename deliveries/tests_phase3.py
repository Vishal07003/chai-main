from django.test import TestCase
from django.contrib.auth.models import User
from decimal import Decimal
from django.utils import timezone
from products.models import Product, Category
from orders.models import Order, OrderItem
from deliveries.models import (
    Delivery,
    DeliveryItem,
    DeliveryPartner,
    DeliveryLocationLog,
    DeliveryStatusHistory,
)
from accounts.models import UserProfile
from rest_framework.test import APIClient
from rest_framework import status

class Phase3DeliveryWorkflowTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        # 1. Users
        self.owner = User.objects.create_user(username='owner_del', password='pw', is_staff=True)
        self.driver1 = User.objects.create_user(username='driver_del1', password='pw')
        self.driver2 = User.objects.create_user(username='driver_del2', password='pw')
        self.customer = User.objects.create_user(username='customer_del', password='pw')

        UserProfile.objects.create(user=self.owner, phone='9000000001', role='owner')
        UserProfile.objects.create(user=self.driver1, phone='9000000002', role='delivery')
        UserProfile.objects.create(user=self.driver2, phone='9000000003', role='delivery')
        UserProfile.objects.create(user=self.customer, phone='9000000004', role='customer')

        # 2. Fleet Partner Profile
        self.partner_profile = DeliveryPartner.objects.create(
            user=self.driver1,
            name='Ramesh Kumar',
            phone='9000000002',
            vehicle_type='E-Bike',
            status='READY'
        )

        # 3. Product & Order
        self.cat = Category.objects.create(name='Drinks')
        self.chai = Product.objects.create(name='Ginger Chai Flask', price=Decimal('120.00'), category=self.cat)
        self.order = Order.objects.create(
            customer=self.customer,
            delivery_address='DLF CyberCity Gate 2',
            total_amount=Decimal('240.00'),
            status='accepted'
        )
        self.order_item = OrderItem.objects.create(
            order=self.order, product=self.chai, quantity=2, price=Decimal('120.00')
        )

        # 4. Delivery assignment container
        self.delivery = Delivery.objects.create(
            order=self.order,
            driver=self.driver1,
            status='assigned'
        )

    def test_p3_1_delivery_partner_only_sees_assigned_deliveries(self):
        """P3-1: Drivers can only view deliveries assigned to their account"""
        self.client.force_authenticate(user=self.driver1)
        res1 = self.client.get('/api/v1/deliveries/')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res1.data), 1)

        self.client.force_authenticate(user=self.driver2)
        res2 = self.client.get('/api/v1/deliveries/')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res2.data), 0)

    def test_p3_2_delivery_status_transitions(self):
        """P3-2: Valid status progression (assigned -> accepted -> picked_up)"""
        self.client.force_authenticate(user=self.driver1)

        # Driver accepts
        res = self.client.patch(f'/api/v1/deliveries/{self.delivery.id}/', {'status': 'accepted'})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'accepted')

        # Driver picks up
        res = self.client.patch(f'/api/v1/deliveries/{self.delivery.id}/', {'status': 'picked_up'})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'picked_up')
        self.assertIsNotNone(self.delivery.pickup_time)

    def test_p3_3_illegal_status_transition_rejected(self):
        """P3-3: Illegal status jump (assigned directly to delivered) is rejected"""
        self.client.force_authenticate(user=self.driver1)
        res = self.client.patch(f'/api/v1/deliveries/{self.delivery.id}/', {'status': 'delivered'})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_p3_4_record_delivered_quantities(self):
        """P3-4: Driver records actual delivered quantities with discrepancy notes"""
        self.client.force_authenticate(user=self.driver1)
        payload = {
            'items': [
                {'product_id': self.chai.id, 'delivered_quantity': 1, 'discrepancy_notes': '1 damaged flask'}
            ]
        }
        res = self.client.post(f'/api/v1/deliveries/{self.delivery.id}/record-delivered-quantities/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        d_item = DeliveryItem.objects.get(delivery=self.delivery, product=self.chai)
        self.assertEqual(d_item.delivered_quantity, 1)
        self.assertEqual(d_item.ordered_quantity, 2)
        self.assertTrue(d_item.has_discrepancy)

    def test_p3_5_location_pings_buffered_and_recorded(self):
        """P3-5: Location updates recorded during active delivery"""
        self.client.force_authenticate(user=self.driver1)
        self.delivery.status = 'on_the_way'
        self.delivery.save()

        payload = {
            'pings': [
                {'latitude': 12.9716, 'longitude': 77.5946, 'accuracy': 5.0, 'speed': 15.0},
                {'latitude': 12.9720, 'longitude': 77.5950, 'accuracy': 4.0, 'speed': 18.0},
            ]
        }
        res = self.client.post(f'/api/v1/deliveries/{self.delivery.id}/location-ping/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['pings_recorded'], 2)

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.current_lat, 12.9720)
        self.assertEqual(self.delivery.current_lng, 77.5950)
        self.assertEqual(DeliveryLocationLog.objects.filter(delivery=self.delivery).count(), 2)

    def test_p3_6_tracking_terminates_after_delivery_completed(self):
        """P3-6: Location pings ignored and active tracking terminated after delivered"""
        self.client.force_authenticate(user=self.driver1)
        self.delivery.status = 'delivered'
        self.delivery.save()

        payload = {'latitude': 12.9730, 'longitude': 77.5960}
        res = self.client.post(f'/api/v1/deliveries/{self.delivery.id}/location-ping/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.data['active_tracking'])
        self.assertEqual(DeliveryLocationLog.objects.filter(delivery=self.delivery).count(), 0)
