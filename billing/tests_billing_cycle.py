from decimal import Decimal
import calendar
from datetime import date, datetime, time, timedelta
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework.exceptions import ValidationError

from billing.models import (
    Customer, Company, Invoice, InvoiceItem, Payment, PaymentAllocation
)
from billing.serializers import CustomerSerializer
from billing.monthly_service import MonthlyBillingService
from orders.models import Order, OrderItem
from products.models import Product, Category
from deliveries.models import Delivery, DeliveryItem


class CustomerBillingCycleTests(TestCase):
    """
    Step 1: Backend tests for Customer-wise Billing Cycle support.
    Verifies:
    1. Default billing cycle day is 1.
    2. Valid values (1..31) accepted via model and serializer.
    3. Invalid values (0, 32, negative, strings) rejected by serializer.
    4. Accurate billing period calculation (day 10, day 5, day 15, day 31, etc.).
    5. Shorter month clamping (day 31 in 30-day month -> 30).
    6. February clamping for non-leap and leap years (2023 vs 2024).
    7. Consecutive non-overlapping periods.
    8. Multi-customer monthly invoice generation with different billing cycle days.
    9. Inclusion of orders strictly within the customer's specific billing cycle.
    10. Duplicate invoice prevention and historical invoice preservation when cycle day changes.
    """

    def setUp(self):
        # 1. Setup Owner user & authenticated APIClient
        self.owner = User.objects.create_user(
            username='owner_user',
            password='password123',
            first_name='Owner',
            last_name='Boss'
        )
        from accounts.models import UserProfile
        if hasattr(self.owner, 'profile'):
            self.owner.profile.role = 'owner'
            self.owner.profile.save()
        else:
            UserProfile.objects.create(user=self.owner, role='owner', phone='+919999999999')

        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

        # 2. Setup Category and Products
        self.category = Category.objects.create(name='Beverages')
        self.chai = Product.objects.create(
            name='Masala Chai',
            category=self.category,
            price=Decimal('20.00'),
            cost_price=Decimal('8.00'),
            is_available=True
        )

    def _create_delivered_order(self, customer: Customer, delivery_date: date, items_info: list) -> Order:
        """Helper to create a delivered order on a specific date."""
        dt = timezone.make_aware(datetime.combine(delivery_date, time(10, 0)))
        order = Order.objects.create(
            customer=self.owner,
            customer_profile=customer,
            status='delivered',
            scheduled_delivery_date=delivery_date,
            created_at=dt,
        )
        order.created_at = dt
        order.save(update_fields=['created_at'])

        delivery = Delivery.objects.create(
            order=order,
            status='delivered'
        )

        for prod, qty, rate in items_info:
            OrderItem.objects.create(
                order=order,
                product=prod,
                quantity=qty,
                price=rate
            )
            DeliveryItem.objects.create(
                delivery=delivery,
                product=prod,
                ordered_quantity=qty,
                delivered_quantity=qty
            )

        return order

    def test_1_default_billing_cycle_day(self):
        """Customer created without cycle day defaults to 1."""
        c = Customer.objects.create(name='Default Cycle Cust', phone='9876543210')
        self.assertEqual(c.billing_cycle_day, 1)

    def test_2_serializer_valid_cycle_days(self):
        """Serializer accepts valid cycle days: 1, 5, 10, 15, 28, 30, 31."""
        valid_days = [1, 5, 10, 15, 28, 30, 31]
        for day in valid_days:
            ser = CustomerSerializer(data={
                'name': f'Cust Day {day}',
                'phone': f'98765432{day:02d}',
                'billing_cycle_day': day
            })
            self.assertTrue(ser.is_valid(), f"Failed for valid day {day}: {ser.errors}")
            cust = ser.save()
            self.assertEqual(cust.billing_cycle_day, day)

    def test_3_serializer_invalid_cycle_days(self):
        """Serializer rejects 0, 32, -5, and non-numeric strings."""
        invalid_values = [0, 32, -1, -10, 50, 'invalid']
        for val in invalid_values:
            ser = CustomerSerializer(data={
                'name': 'Invalid Day Cust',
                'phone': '9876543299',
                'billing_cycle_day': val
            })
            self.assertFalse(ser.is_valid(), f"Should have failed for invalid day {val}")
            self.assertIn('billing_cycle_day', ser.errors)

    def test_4_api_create_and_update_billing_cycle_day(self):
        """Owner can create and update customer's billing_cycle_day via API."""
        create_resp = self.client.post('/api/v1/customers/', {
            'name': 'API Cust',
            'phone': '9876500001',
            'billing_cycle_day': 10
        }, format='json')
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(create_resp.data['billing_cycle_day'], 10)
        cust_id = create_resp.data['id']

        # Update to day 15
        patch_resp = self.client.patch(f'/api/v1/customers/{cust_id}/', {
            'billing_cycle_day': 15
        }, format='json')
        self.assertEqual(patch_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_resp.data['billing_cycle_day'], 15)

        # Updating with invalid day fails
        invalid_resp = self.client.patch(f'/api/v1/customers/{cust_id}/', {
            'billing_cycle_day': 35
        }, format='json')
        self.assertEqual(invalid_resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_5_period_calculation_standard_cycle_days(self):
        """Test calculation logic for cycle days 1, 5, 10, 15."""
        # Cycle day 1 for Nov 2026: 2026-11-01 to 2026-11-30
        s, e, _, _ = MonthlyBillingService.get_customer_billing_period(1, 2026, 11)
        self.assertEqual(s, date(2026, 11, 1))
        self.assertEqual(e, date(2026, 11, 30))

        # Cycle day 10 for Nov 2026: 2026-11-10 to 2026-12-09
        s, e, _, _ = MonthlyBillingService.get_customer_billing_period(10, 2026, 11)
        self.assertEqual(s, date(2026, 11, 10))
        self.assertEqual(e, date(2026, 12, 9))

        # Cycle day 5 for Nov 2026: 2026-11-05 to 2026-12-04
        s, e, _, _ = MonthlyBillingService.get_customer_billing_period(5, 2026, 11)
        self.assertEqual(s, date(2026, 11, 5))
        self.assertEqual(e, date(2026, 12, 4))

        # Cycle day 15 for Nov 2026: 2026-11-15 to 2026-12-14
        s, e, _, _ = MonthlyBillingService.get_customer_billing_period(15, 2026, 11)
        self.assertEqual(s, date(2026, 11, 15))
        self.assertEqual(e, date(2026, 12, 14))

        # December boundary into next year (Cycle day 10 for Dec 2026 -> Dec 10, 2026 to Jan 9, 2027)
        s, e, _, _ = MonthlyBillingService.get_customer_billing_period(10, 2026, 12)
        self.assertEqual(s, date(2026, 12, 10))
        self.assertEqual(e, date(2027, 1, 9))

    def test_6_month_end_clamping_and_leap_years(self):
        """Test clamping for 30-day months, 31-day months, and February leap vs non-leap years."""
        # Cycle day 31 in April (30 days) -> starts on April 30, next month starts May 31, ends May 30
        s, e, _, _ = MonthlyBillingService.get_customer_billing_period(31, 2026, 4)
        self.assertEqual(s, date(2026, 4, 30))
        self.assertEqual(e, date(2026, 5, 30))

        # Cycle day 31 in January 2023 (Non-leap year: Feb 2023 has 28 days)
        # Next period starts Feb 28, so this period ends Feb 27
        s, e, _, _ = MonthlyBillingService.get_customer_billing_period(31, 2023, 1)
        self.assertEqual(s, date(2023, 1, 31))
        self.assertEqual(e, date(2023, 2, 27))

        # Cycle day 31 in January 2024 (Leap year: Feb 2024 has 29 days)
        # Next period starts Feb 29, so this period ends Feb 28
        s, e, _, _ = MonthlyBillingService.get_customer_billing_period(31, 2024, 1)
        self.assertEqual(s, date(2024, 1, 31))
        self.assertEqual(e, date(2024, 2, 28))

        # Cycle day 31 in February 2024 (starts Feb 29, next month starts March 31, ends March 30)
        s, e, _, _ = MonthlyBillingService.get_customer_billing_period(31, 2024, 2)
        self.assertEqual(s, date(2024, 2, 29))
        self.assertEqual(e, date(2024, 3, 30))

    def test_7_consecutive_periods_non_overlapping(self):
        """Consecutive periods must be contiguous and non-overlapping."""
        for cycle_day in [1, 10, 28, 30, 31]:
            # Oct 2026 and Nov 2026
            s1, e1, _, _ = MonthlyBillingService.get_customer_billing_period(cycle_day, 2026, 10)
            s2, e2, _, _ = MonthlyBillingService.get_customer_billing_period(cycle_day, 2026, 11)
            self.assertEqual(e1 + timedelta(days=1), s2, f"Overlap/gap for cycle day {cycle_day}")

    def test_8_orders_included_strictly_in_customer_cycle(self):
        """Orders outside customer's billing cycle are excluded from that month's invoice."""
        # Customer A: cycle day 10 -> Nov 2026 cycle: Nov 10 to Dec 9
        custA = Customer.objects.create(name='Cust A', phone='9876500010', billing_cycle_day=10)

        # Order 1: Nov 5 (Before cycle start: belongs to Oct period)
        o_before = self._create_delivered_order(custA, date(2026, 11, 5), [(self.chai, 5, Decimal('20.00'))])
        # Order 2: Nov 10 (On cycle start: in Nov period)
        o_start = self._create_delivered_order(custA, date(2026, 11, 10), [(self.chai, 4, Decimal('20.00'))])
        # Order 3: Dec 9 (On cycle end: in Nov period)
        o_end = self._create_delivered_order(custA, date(2026, 12, 9), [(self.chai, 6, Decimal('20.00'))])
        # Order 4: Dec 10 (After cycle end: belongs to Dec period)
        o_after = self._create_delivered_order(custA, date(2026, 12, 10), [(self.chai, 7, Decimal('20.00'))])

        # Generate invoice for Nov 2026
        inv = MonthlyBillingService.generate_customer_monthly_invoice(
            customer=custA,
            year=2026,
            month=11,
            owner_user=self.owner
        )

        self.assertIsNotNone(inv)
        attached_order_ids = set(inv.invoice_orders.values_list('order_id', flat=True))
        self.assertNotIn(o_before.id, attached_order_ids)
        self.assertIn(o_start.id, attached_order_ids)
        self.assertIn(o_end.id, attached_order_ids)
        self.assertNotIn(o_after.id, attached_order_ids)

        # Expected total: (4 * 20) + (6 * 20) = 80 + 120 = 200
        self.assertEqual(inv.grand_total, Decimal('200.00'))

    def test_9_multi_customer_different_billing_cycle_days(self):
        """Batch generation correctly segregates orders for customers with different cycle days."""
        # Customer 1 (day 1): Nov 1 to Nov 30
        cust1 = Customer.objects.create(name='Day 1 Cust', phone='9876500001', billing_cycle_day=1)
        # Customer 2 (day 15): Nov 15 to Dec 14
        cust2 = Customer.objects.create(name='Day 15 Cust', phone='9876500015', billing_cycle_day=15)

        # Order on Nov 5: Valid for cust1, but NOT for cust2 Nov cycle
        o1 = self._create_delivered_order(cust1, date(2026, 11, 5), [(self.chai, 10, Decimal('20.00'))])  # 200
        o2_early = self._create_delivered_order(cust2, date(2026, 11, 5), [(self.chai, 10, Decimal('20.00'))])

        # Order on Dec 5: NOT for cust1 Nov cycle, but Valid for cust2 Nov cycle
        o2_in_cycle = self._create_delivered_order(cust2, date(2026, 12, 5), [(self.chai, 5, Decimal('20.00'))])  # 100

        invoices = MonthlyBillingService.generate_monthly_invoices_for_month(
            year=2026,
            month=11,
            owner_user=self.owner,
            customer_ids=[cust1.id, cust2.id]
        )

        self.assertEqual(len(invoices), 2)
        inv1 = next(i for i in invoices if i.customer_id == cust1.id)
        inv2 = next(i for i in invoices if i.customer_id == cust2.id)

        self.assertEqual(inv1.grand_total, Decimal('200.00'))
        self.assertIn(o1.id, inv1.invoice_orders.values_list('order_id', flat=True))

        self.assertEqual(inv2.grand_total, Decimal('100.00'))
        self.assertIn(o2_in_cycle.id, inv2.invoice_orders.values_list('order_id', flat=True))
        self.assertNotIn(o2_early.id, inv2.invoice_orders.values_list('order_id', flat=True))

    def test_10_changing_cycle_day_preserves_historical_invoices(self):
        """Changing customer's cycle day preserves historical invoices and prevents duplicate regeneration."""
        cust = Customer.objects.create(name='Changing Cust', phone='9876500050', billing_cycle_day=1)
        self._create_delivered_order(cust, date(2026, 11, 5), [(self.chai, 10, Decimal('20.00'))])

        # Generate invoice for Nov 2026 with cycle day 1
        inv = MonthlyBillingService.generate_customer_monthly_invoice(
            customer=cust,
            year=2026,
            month=11,
            owner_user=self.owner
        )
        self.assertEqual(inv.grand_total, Decimal('200.00'))
        orig_inv_num = inv.invoice_number

        # Owner changes customer's cycle day to 10
        cust.billing_cycle_day = 10
        cust.save(update_fields=['billing_cycle_day'])

        # Attempting to re-generate for 2026-11 skips/prevents duplicate
        re_generated = MonthlyBillingService.generate_monthly_invoices_for_month(
            year=2026,
            month=11,
            owner_user=self.owner,
            customer_ids=[cust.id]
        )
        self.assertEqual(len(re_generated), 0)

        # Historical invoice is intact
        inv.refresh_from_db()
        self.assertEqual(inv.invoice_number, orig_inv_num)
        self.assertEqual(inv.grand_total, Decimal('200.00'))
        self.assertEqual(inv.billing_period, '2026-11')
