from datetime import date, time, timedelta
from decimal import Decimal
import zoneinfo

from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import UserProfile
from billing.models import Customer
from products.models import Product
from deliveries.models import DeliveryPartner, Delivery
from orders.models import (
    Order,
    OrderItem,
    DailyDeliverySchedule,
    DailyDeliveryScheduleItem,
    DailyScheduleOrderExecution,
    ScheduleCancellationException,
    ScheduleNotificationTracker,
)
from orders.daily_schedule_service import DailyScheduleService

KOLKATA_TZ = zoneinfo.ZoneInfo('Asia/Kolkata')


class DailyDeliveryScheduleTests(TestCase):
    def setUp(self):
        # 1. Owner
        self.owner_user = User.objects.create_user(username='test_owner', password='password123')
        UserProfile.objects.create(user=self.owner_user, role='owner', phone='9999900001')

        # 2. Driver 1 & Partner 1
        self.driver_user1 = User.objects.create_user(username='driver_one', password='password123', first_name='Ramesh', last_name='Kumar')
        UserProfile.objects.create(user=self.driver_user1, role='delivery', phone='9999900002')
        self.partner1 = DeliveryPartner.objects.create(
            user=self.driver_user1,
            name='Ramesh Kumar',
            phone='9999900002',
            vehicle_type='BIKE'
        )

        # 3. Driver 2 & Partner 2
        self.driver_user2 = User.objects.create_user(username='driver_two', password='password123', first_name='Suresh', last_name='Verma')
        UserProfile.objects.create(user=self.driver_user2, role='delivery', phone='9999900003')
        self.partner2 = DeliveryPartner.objects.create(
            user=self.driver_user2,
            name='Suresh Verma',
            phone='9999900003',
            vehicle_type='SCOOTER'
        )

        # 4. Customers
        self.cust_user1 = User.objects.create_user(username='client1', password='password123')
        self.customer1 = Customer.objects.create(
            name='Infosys Hub',
            phone='9876543210',
            address='Floor 3, Tech Park'
        )

        self.cust_user2 = User.objects.create_user(username='client2', password='password123')
        self.customer2 = Customer.objects.create(
            name='Sharma & Co',
            phone='9876543211',
            address='Suite 101, Business Tower'
        )

        # 5. Products
        self.chai = Product.objects.create(name='Masala Chai', price=Decimal('20.00'), is_available=True)
        self.bun = Product.objects.create(name='Bun Maska', price=Decimal('40.00'), is_available=True)

    def test_schedule_creation_two_different_customers_and_times(self):
        """Test two customers with different delivery times, weekdays, and fixed partners."""
        today = timezone.now().astimezone(KOLKATA_TZ).date()

        # Customer 1: 10:00 AM, Mon-Fri, Partner 1
        sched1 = DailyDeliverySchedule.objects.create(
            customer_profile=self.customer1,
            customer_user=self.cust_user1,
            delivery_partner=self.partner1,
            delivery_time=time(10, 0),
            weekdays='0,1,2,3,4',
            start_date=today,
            is_active=True
        )
        DailyDeliveryScheduleItem.objects.create(schedule=sched1, product=self.chai, quantity=4, unit_price=Decimal('20.00'))

        # Customer 2: 03:30 PM, Everyday, Partner 2
        sched2 = DailyDeliverySchedule.objects.create(
            customer_profile=self.customer2,
            customer_user=self.cust_user2,
            delivery_partner=self.partner2,
            delivery_time=time(15, 30),
            weekdays='0,1,2,3,4,5,6',
            start_date=today,
            is_active=True
        )
        DailyDeliveryScheduleItem.objects.create(schedule=sched2, product=self.chai, quantity=2, unit_price=Decimal('20.00'))
        DailyDeliveryScheduleItem.objects.create(schedule=sched2, product=self.bun, quantity=1, unit_price=Decimal('40.00'))

        self.assertEqual(DailyDeliverySchedule.objects.count(), 2)
        self.assertEqual(sched1.delivery_partner.name, 'Ramesh Kumar')
        self.assertEqual(sched2.delivery_partner.name, 'Suresh Verma')
        self.assertEqual(sched1.delivery_time, time(10, 0))
        self.assertEqual(sched2.delivery_time, time(15, 30))

    def test_automatic_order_generation_and_idempotency(self):
        """Test automatic order generation and idempotency (no duplicate orders)."""
        today = timezone.now().astimezone(KOLKATA_TZ).date()
        sched = DailyDeliverySchedule.objects.create(
            customer_profile=self.customer1,
            customer_user=self.cust_user1,
            delivery_partner=self.partner1,
            delivery_time=time(10, 0),
            weekdays=str(today.weekday()),
            start_date=today,
            is_active=True
        )
        DailyDeliveryScheduleItem.objects.create(schedule=sched, product=self.chai, quantity=5, unit_price=Decimal('20.00'))

        # Run 1: Should generate exactly 1 order
        res1 = DailyScheduleService.generate_orders_for_date(today)
        self.assertEqual(res1['generated_count'], 1)
        self.assertEqual(Order.objects.count(), 1)

        order = Order.objects.first()
        self.assertEqual(order.total_amount, Decimal('100.00'))
        self.assertEqual(order.scheduled_delivery_date, today)
        self.assertEqual(order.scheduled_delivery_time, time(10, 0))
        self.assertEqual(order.delivery.driver, self.driver_user1)

        # Run 2: Immediately run again for the same date -> Idempotency prevents duplicates
        res2 = DailyScheduleService.generate_orders_for_date(today)
        self.assertEqual(res2['generated_count'], 0)
        self.assertEqual(res2['skipped_existing_count'], 1)
        self.assertEqual(Order.objects.count(), 1)

    def test_cancellation_before_order_generation_case_a(self):
        """CASE A: Cancel date before order is generated -> logged exception & skips generation."""
        target_date = timezone.now().astimezone(KOLKATA_TZ).date() + timedelta(days=1)
        sched = DailyDeliverySchedule.objects.create(
            customer_profile=self.customer1,
            customer_user=self.cust_user1,
            delivery_partner=self.partner1,
            delivery_time=time(10, 0),
            weekdays=str(target_date.weekday()),
            start_date=target_date,
            is_active=True
        )
        DailyDeliveryScheduleItem.objects.create(schedule=sched, product=self.chai, quantity=2)

        # Cancel date
        res = DailyScheduleService.cancel_date_delivery(sched, target_date, reason="Office closed for holiday")
        self.assertEqual(res['case'], 'A')
        self.assertTrue(ScheduleCancellationException.objects.filter(schedule=sched, cancelled_date=target_date).exists())

        # Attempt to run generator for target_date -> Must be skipped
        gen_res = DailyScheduleService.generate_orders_for_date(target_date)
        self.assertEqual(gen_res['generated_count'], 0)
        self.assertEqual(Order.objects.count(), 0)

        # Future date remains eligible
        next_day = target_date + timedelta(days=7) # same weekday next week
        self.assertTrue(sched.is_eligible_for_date(next_day))

    def test_cancellation_after_order_generation_case_b(self):
        """CASE B: Cancel date after order generated -> marks order cancelled, notifies partner."""
        today = timezone.now().astimezone(KOLKATA_TZ).date()
        sched = DailyDeliverySchedule.objects.create(
            customer_profile=self.customer1,
            customer_user=self.cust_user1,
            delivery_partner=self.partner1,
            delivery_time=time(10, 0),
            weekdays=str(today.weekday()),
            start_date=today,
            is_active=True
        )
        DailyDeliveryScheduleItem.objects.create(schedule=sched, product=self.chai, quantity=3)

        # Generate order
        DailyScheduleService.generate_orders_for_date(today)
        order = Order.objects.first()
        self.assertEqual(order.status, 'pending')

        # Cancel after generation
        res = DailyScheduleService.cancel_date_delivery(sched, today, reason="Client sudden meeting cancellation")
        self.assertEqual(res['case'], 'B')
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(order.delivery.status, 'failed')
        self.assertTrue(order.schedule_notification_tracker.is_cancelled)

    def test_inactive_and_end_date_schedules(self):
        """Inactive schedules or schedules whose end date has passed should not generate orders."""
        today = timezone.now().astimezone(KOLKATA_TZ).date()
        yesterday = today - timedelta(days=1)

        # Inactive schedule
        sched_inactive = DailyDeliverySchedule.objects.create(
            customer_profile=self.customer1,
            customer_user=self.cust_user1,
            delivery_partner=self.partner1,
            delivery_time=time(10, 0),
            weekdays=str(today.weekday()),
            start_date=today,
            is_active=False
        )
        DailyDeliveryScheduleItem.objects.create(schedule=sched_inactive, product=self.chai, quantity=1)

        # Expired schedule
        sched_expired = DailyDeliverySchedule.objects.create(
            customer_profile=self.customer2,
            customer_user=self.cust_user2,
            delivery_partner=self.partner2,
            delivery_time=time(11, 0),
            weekdays=str(today.weekday()),
            start_date=today - timedelta(days=10),
            end_date=yesterday,
            is_active=True
        )
        DailyDeliveryScheduleItem.objects.create(schedule=sched_expired, product=self.chai, quantity=1)

        res = DailyScheduleService.generate_orders_for_date(today)
        self.assertEqual(res['generated_count'], 0)
        self.assertEqual(Order.objects.count(), 0)

    def test_partner_acceptance_stops_reminders(self):
        """Partner acceptance flags tracker and prevents reminders from being sent."""
        today = timezone.now().astimezone(KOLKATA_TZ).date()
        sched = DailyDeliverySchedule.objects.create(
            customer_profile=self.customer1,
            customer_user=self.cust_user1,
            delivery_partner=self.partner1,
            delivery_time=time(10, 0),
            weekdays=str(today.weekday()),
            start_date=today,
            is_active=True
        )
        DailyDeliveryScheduleItem.objects.create(schedule=sched, product=self.chai, quantity=2)
        DailyScheduleService.generate_orders_for_date(today)

        order = Order.objects.first()
        delivery = order.delivery
        delivery.status = 'accepted'
        delivery.save()

        # Run notification processor
        res = DailyScheduleService.process_notification_reminders()
        tracker = order.schedule_notification_tracker
        tracker.refresh_from_db()
        self.assertIsNotNone(tracker.partner_accepted_at)
        self.assertIsNone(tracker.reminder_notified_at)
