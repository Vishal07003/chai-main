import zoneinfo
from datetime import date, time, datetime, timedelta
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone

from billing.models import Company, Customer, Payment
from products.models import Product
from deliveries.models import DeliveryPartner, Delivery
from accounts.models import UserProfile
from orders.models import Order, OrderItem, DailyDeliverySchedule, DailyDeliveryScheduleItem, DailyScheduleOrderExecution
from orders.serializers import OrderSerializer, OrderCreateSerializer
from notifications.models import NotificationLog

KOLKATA_TZ = zoneinfo.ZoneInfo('Asia/Kolkata')


class CustomerProfileWorkflowTestCase(TestCase):
    def setUp(self):
        # 1. Users
        self.owner = User.objects.create_user(username='owner_user', password='password123')
        UserProfile.objects.create(user=self.owner, role='owner', phone='9999900001')

        self.partner_user = User.objects.create_user(username='partner_user', password='password123')
        UserProfile.objects.create(user=self.partner_user, role='delivery', phone='9876543210')

        self.delivery_partner = DeliveryPartner.objects.create(
            user=self.partner_user,
            name='Ramesh Kumar',
            phone='9876543210',
            vehicle_type='Bike'
        )

        # 2. Customers A and B
        self.customer_a = Customer.objects.create(
            name='Customer Alpha',
            phone='9111111111',
            address='Alpha Towers, Floor 3'
        )
        self.customer_b = Customer.objects.create(
            name='Customer Beta',
            phone='9222222222',
            address='Beta Complex, Shop 10'
        )

        # 3. Products
        self.product_chai = Product.objects.create(
            name='Masala Chai (Flask)',
            price=Decimal('150.00'),
            is_available=True
        )
        self.product_bun = Product.objects.create(
            name='Bun Maska',
            price=Decimal('40.00'),
            is_available=True
        )

    def test_customer_order_filtering_isolation(self):
        """Verify Customer A sees strictly Customer A orders, and Customer B sees only Customer B."""
        # Order for Customer A
        order_a = Order.objects.create(
            customer=self.owner,
            customer_profile=self.customer_a,
            total_amount=Decimal('150.00'),
            delivery_address=self.customer_a.address
        )
        OrderItem.objects.create(order=order_a, product=self.product_chai, quantity=1, price=Decimal('150.00'))

        # Order for Customer B
        order_b = Order.objects.create(
            customer=self.owner,
            customer_profile=self.customer_b,
            total_amount=Decimal('80.00'),
            delivery_address=self.customer_b.address
        )
        OrderItem.objects.create(order=order_b, product=self.product_bun, quantity=2, price=Decimal('40.00'))

        # Filter strictly by customer_profile_id
        orders_for_a = Order.objects.filter(customer_profile=self.customer_a)
        orders_for_b = Order.objects.filter(customer_profile=self.customer_b)

        self.assertEqual(orders_for_a.count(), 1)
        self.assertEqual(orders_for_a.first().id, order_a.id)

        self.assertEqual(orders_for_b.count(), 1)
        self.assertEqual(orders_for_b.first().id, order_b.id)

    def test_multiple_orders_on_same_date(self):
        """Verify multiple orders for same customer on same date display as separate records."""
        today = date.today()
        # Order 1: Morning 9:00 AM
        order_1 = Order.objects.create(
            customer=self.owner,
            customer_profile=self.customer_a,
            total_amount=Decimal('150.00'),
            scheduled_delivery_date=today,
            scheduled_delivery_time=time(9, 0),
            delivery_address=self.customer_a.address
        )
        # Order 2: Evening 4:00 PM
        order_2 = Order.objects.create(
            customer=self.owner,
            customer_profile=self.customer_a,
            total_amount=Decimal('300.00'),
            scheduled_delivery_date=today,
            scheduled_delivery_time=time(16, 0),
            delivery_address=self.customer_a.address
        )

        cust_orders = Order.objects.filter(customer_profile=self.customer_a).order_by('scheduled_delivery_time')
        self.assertEqual(cust_orders.count(), 2)
        self.assertNotEqual(order_1.id, order_2.id)
        self.assertEqual(cust_orders[0].scheduled_delivery_time, time(9, 0))
        self.assertEqual(cust_orders[1].scheduled_delivery_time, time(16, 0))

    def test_one_time_order_creation_serializer(self):
        """Verify One-Time Order creates exactly one order, never a recurring schedule."""
        from rest_framework.test import APIRequestFactory
        factory = APIRequestFactory()
        request = factory.post('/api/v1/orders/')
        request.user = self.owner

        payload = {
            'customer_id': self.customer_a.id,
            'delivery_partner_id': self.delivery_partner.id,
            'scheduled_delivery_date': str(date.today()),
            'scheduled_delivery_time': '11:00:00',
            'delivery_address': 'Alpha Tech Park',
            'items': [
                {
                    'product_id': self.product_chai.id,
                    'quantity': 2,
                    'requested_price': '140.00',
                    'discount_reason': 'Special customer rate'
                },
                {
                    'product_id': self.product_bun.id,
                    'quantity': 1,
                    'requested_price': '40.00'
                }
            ]
        }

        serializer = OrderCreateSerializer(data=payload, context={'request': request})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        order = serializer.save()

        self.assertIsNotNone(order.id)
        self.assertEqual(order.customer_profile, self.customer_a)
        self.assertEqual(order.items.count(), 2)
        self.assertEqual(DailyDeliverySchedule.objects.count(), 0)  # Never creates schedule

        # Check delivery partner assigned
        self.assertTrue(hasattr(order, 'delivery'))
        self.assertEqual(order.delivery.driver, self.partner_user)

    def test_immediate_vs_future_notification_dispatch(self):
        """Verify immediate delivery triggers notification immediately, future delivery defers."""
        from rest_framework.test import APIRequestFactory
        factory = APIRequestFactory()
        request = factory.post('/api/v1/orders/')
        request.user = self.owner

        now_dt = timezone.now().astimezone(KOLKATA_TZ)
        today = now_dt.date()

        # 1. Immediate order (today, within 15 minutes)
        target_time = (now_dt + timedelta(minutes=10)).time()
        payload_imm = {
            'customer_id': self.customer_a.id,
            'delivery_partner_id': self.delivery_partner.id,
            'scheduled_delivery_date': str(today),
            'scheduled_delivery_time': target_time.strftime('%H:%M:%S'),
            'delivery_address': 'Alpha Tech Park',
            'items': [{'product_id': self.product_chai.id, 'quantity': 1}]
        }
        with self.captureOnCommitCallbacks(execute=True):
            ser_imm = OrderCreateSerializer(data=payload_imm, context={'request': request})
            self.assertTrue(ser_imm.is_valid())
            order_imm = ser_imm.save()

        # Immediate notification should have been logged for partner (FCM alert)
        partner_notifs = NotificationLog.objects.filter(
            recipient=self.partner_user,
            channel='fcm'
        )
        self.assertGreater(partner_notifs.count(), 0)

        # 2. Future order (tomorrow)
        tomorrow = today + timedelta(days=1)
        payload_fut = {
            'customer_id': self.customer_a.id,
            'delivery_partner_id': self.delivery_partner.id,
            'scheduled_delivery_date': str(tomorrow),
            'scheduled_delivery_time': '10:00:00',
            'delivery_address': 'Alpha Tech Park',
            'items': [{'product_id': self.product_chai.id, 'quantity': 1}]
        }
        fut_notif_count_before = NotificationLog.objects.filter(
            recipient=self.partner_user,
            channel='fcm'
        ).count()
        with self.captureOnCommitCallbacks(execute=True):
            ser_fut = OrderCreateSerializer(data=payload_fut, context={'request': request})
            self.assertTrue(ser_fut.is_valid())
            order_fut = ser_fut.save()

        # Future order does NOT dispatch partner delivery assignment notification immediately
        fut_notif_count_after = NotificationLog.objects.filter(
            recipient=self.partner_user,
            channel='fcm'
        ).count()
        self.assertEqual(fut_notif_count_after, fut_notif_count_before)
