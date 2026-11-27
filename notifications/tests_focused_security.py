from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from accounts.models import UserProfile
from billing.models import Payment, Invoice, Customer
from deliveries.models import Delivery
from orders.models import Order
from notifications.models import DeviceToken, NotificationLog
from notifications.services import NotificationService


class NotificationSecurityAndRoleTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        # 1. Business Owner
        self.owner = User.objects.create_user(username='tea_owner', password='Password123!', is_staff=True)
        self.owner_profile = UserProfile.objects.create(user=self.owner, phone='9876543210', role='owner')

        # 2. Delivery Partner 1
        self.partner1 = User.objects.create_user(username='partner_one', password='Password123!')
        self.partner1_profile = UserProfile.objects.create(user=self.partner1, phone='9123456781', role='delivery')

        # 3. Delivery Partner 2
        self.partner2 = User.objects.create_user(username='partner_two', password='Password123!')
        self.partner2_profile = UserProfile.objects.create(user=self.partner2, phone='9123456782', role='delivery')

        # 4. Customer
        self.customer_user = User.objects.create_user(username='chai_lover', password='Password123!')
        self.customer_profile = UserProfile.objects.create(user=self.customer_user, phone='9988776655', role='customer')
        self.customer = Customer.objects.create(name='Tech Park Cafe', phone='9988776655')

    def test_owner_receives_payment_notification_and_partner_does_not(self):
        """Owner receives payment notification in their history; Delivery Partners never receive it."""
        payment = Payment.objects.create(
            amount=Decimal('450.00'),
            method='upi',
            customer=self.customer,
            recorded_by=self.owner
        )
        with self.captureOnCommitCallbacks(execute=True):
            NotificationService.on_payment_received(payment, customer_name='Tech Park Cafe')

        # Check Owner History
        self.client.force_authenticate(user=self.owner)
        owner_res = self.client.get('/api/v1/notifications/history/')
        self.assertEqual(owner_res.status_code, status.HTTP_200_OK)
        owner_titles = [n['title'] for n in owner_res.data]
        self.assertTrue(any('Payment Received' in t for t in owner_titles))

        # Check Partner 1 History
        self.client.force_authenticate(user=self.partner1)
        p1_res = self.client.get('/api/v1/notifications/history/')
        self.assertEqual(p1_res.status_code, status.HTTP_200_OK)
        p1_titles = [n['title'] for n in p1_res.data]
        self.assertFalse(any('Payment Received' in t for t in p1_titles))

        # Check Partner 2 History
        self.client.force_authenticate(user=self.partner2)
        p2_res = self.client.get('/api/v1/notifications/history/')
        self.assertEqual(p2_res.status_code, status.HTTP_200_OK)
        p2_titles = [n['title'] for n in p2_res.data]
        self.assertFalse(any('Payment Received' in t for t in p2_titles))

    def test_owner_receives_invoice_notification_and_partner_does_not(self):
        """Owner receives invoice generated notification; Delivery Partners never receive it."""
        invoice = Invoice.objects.create(
            customer=self.customer,
            invoice_number='INV-TEST-001',
            subtotal=Decimal('500.00'),
            grand_total=Decimal('500.00')
        )
        with self.captureOnCommitCallbacks(execute=True):
            NotificationService.on_invoice_generated(invoice)

        # Check Owner History
        self.client.force_authenticate(user=self.owner)
        owner_res = self.client.get('/api/v1/notifications/history/')
        self.assertEqual(owner_res.status_code, status.HTTP_200_OK)
        owner_titles = [n['title'] for n in owner_res.data]
        self.assertTrue(any('Invoice Generated' in t for t in owner_titles))

        # Check Partner 1 History
        self.client.force_authenticate(user=self.partner1)
        p1_res = self.client.get('/api/v1/notifications/history/')
        self.assertEqual(p1_res.status_code, status.HTTP_200_OK)
        p1_titles = [n['title'] for n in p1_res.data]
        self.assertFalse(any('Invoice Generated' in t for t in p1_titles))

    def test_delivery_partner_receives_assigned_order_notification(self):
        """Delivery Partner receives notification strictly for orders assigned to them."""
        order = Order.objects.create(
            customer=self.customer_user,
            customer_profile=self.customer,
            total_amount=Decimal('120.00'),
            delivery_address='Block B, Floor 3'
        )
        delivery = Delivery.objects.create(
            order=order,
            driver=self.partner1,
            status='assigned'
        )
        with self.captureOnCommitCallbacks(execute=True):
            NotificationService.on_delivery_assigned(delivery)

        # Partner 1 must see this delivery
        self.client.force_authenticate(user=self.partner1)
        p1_res = self.client.get('/api/v1/notifications/history/')
        self.assertEqual(p1_res.status_code, status.HTTP_200_OK)
        p1_titles = [n['title'] for n in p1_res.data]
        self.assertTrue(any('New Delivery Assigned' in t for t in p1_titles))

    def test_partner_does_not_receive_another_partners_order_notification(self):
        """Partner 2 must NOT receive Partner 1's delivery assignment."""
        order = Order.objects.create(
            customer=self.customer_user,
            customer_profile=self.customer,
            total_amount=Decimal('200.00'),
            delivery_address='Block C, Floor 1'
        )
        delivery = Delivery.objects.create(
            order=order,
            driver=self.partner1,
            status='assigned'
        )
        with self.captureOnCommitCallbacks(execute=True):
            NotificationService.on_delivery_assigned(delivery)

        # Partner 2 checks inbox
        self.client.force_authenticate(user=self.partner2)
        p2_res = self.client.get('/api/v1/notifications/history/')
        self.assertEqual(p2_res.status_code, status.HTTP_200_OK)
        p2_titles = [n['title'] for n in p2_res.data]
        self.assertFalse(any('New Delivery Assigned' in t for t in p2_titles))

    def test_unassigned_order_cannot_generate_notification_for_partner(self):
        """An unassigned order (driver=None) does not send notification to any partner."""
        order = Order.objects.create(
            customer=self.customer_user,
            customer_profile=self.customer,
            total_amount=Decimal('80.00')
        )
        delivery = Delivery.objects.create(
            order=order,
            driver=None,
            status='pending'
        )
        count_before = NotificationLog.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            NotificationService.on_delivery_assigned(delivery)
        count_after = NotificationLog.objects.count()
        self.assertEqual(count_before, count_after)

    def test_duplicate_notification_prevention(self):
        """Triggering the same event twice with the same dedup_key prevents duplicate dispatch."""
        order = Order.objects.create(
            customer=self.customer_user,
            customer_profile=self.customer,
            total_amount=Decimal('150.00'),
            delivery_address='Tower A'
        )
        delivery = Delivery.objects.create(
            order=order,
            driver=self.partner1,
            status='assigned'
        )
        with self.captureOnCommitCallbacks(execute=True):
            NotificationService.on_delivery_assigned(delivery)
        notif_count_1 = NotificationLog.objects.filter(recipient=self.partner1).count()
        self.assertGreaterEqual(notif_count_1, 1)

        # Trigger again
        with self.captureOnCommitCallbacks(execute=True):
            NotificationService.on_delivery_assigned(delivery)
        notif_count_2 = NotificationLog.objects.filter(recipient=self.partner1).count()
        self.assertEqual(notif_count_1, notif_count_2)

    def test_unauthorized_notification_history_access_is_rejected(self):
        """Unauthenticated requests to notification history are rejected with HTTP 401."""
        self.client.logout()
        res = self.client.get('/api/v1/notifications/history/')
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
