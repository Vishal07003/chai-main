from decimal import Decimal
import calendar
from datetime import date, datetime, time
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from billing.models import (
    Customer, Company, Invoice, InvoiceItem, Payment, PaymentAllocation, InvoiceAuditLog
)
from billing.monthly_service import MonthlyBillingService
from billing.payment_service import PaymentAllocationService
from billing.ledger_service import LedgerService
from orders.models import Order, OrderItem
from products.models import Product, Category
from deliveries.models import Delivery, DeliveryItem


class MonthlyBillingBackendTests(TestCase):
    def setUp(self):
        # 1. Setup Owner user & Client
        self.owner = User.objects.create_user(
            username='owner_user',
            password='password123',
            first_name='Owner',
            last_name='Tester'
        )
        # Give owner role
        from accounts.models import UserProfile
        if hasattr(self.owner, 'profile'):
            self.owner.profile.role = 'owner'
            self.owner.profile.save()
        else:
            UserProfile.objects.create(user=self.owner, role='owner', phone='+919999999999')

        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

        # 2. Setup Non-owner user
        self.driver_user = User.objects.create_user(
            username='driver_user',
            password='password123'
        )
        UserProfile.objects.create(user=self.driver_user, role='delivery', phone='+918888888888')

        # 3. Setup Customers
        self.customer1 = Customer.objects.create(
            name='Alpha Corp',
            phone='+919876543210',
            address='Tech Park, Block A'
        )
        self.customer2 = Customer.objects.create(
            name='Beta Solutions',
            phone='+919876543211',
            address='Tech Park, Block B'
        )

        # 4. Setup Products
        self.category = Category.objects.create(name='Beverages')
        self.chai = Product.objects.create(
            name='Masala Chai',
            category=self.category,
            price=Decimal('20.00'),
            cost_price=Decimal('8.00'),
            is_available=True
        )
        self.coffee = Product.objects.create(
            name='Filter Coffee',
            category=self.category,
            price=Decimal('30.00'),
            cost_price=Decimal('12.00'),
            is_available=True
        )

    def _create_delivered_order(self, customer, order_date: date, items: list, status='delivered'):
        """Helper to create an order with delivery in delivered status"""
        order = Order.objects.create(
            customer=self.owner,
            customer_profile=customer,
            status=status,
            total_amount=Decimal('0.00'),
            scheduled_delivery_date=order_date,
            delivery_address=customer.address
        )
        total = Decimal('0.00')
        for prod, qty, rate in items:
            OrderItem.objects.create(
                order=order,
                product=prod,
                quantity=qty,
                price=rate
            )
            total += (rate * qty)
        order.total_amount = total
        order.save()

        # Create delivery & delivered items
        delivery = Delivery.objects.create(
            order=order,
            status=status
        )
        for prod, qty, rate in items:
            DeliveryItem.objects.create(
                delivery=delivery,
                product=prod,
                ordered_quantity=qty,
                delivered_quantity=qty
            )

        return order

    def test_1_monthly_invoice_generation_multiple_orders(self):
        """1 & 2: Monthly invoice generation groups multiple delivered orders for one customer."""
        d1 = date(2026, 11, 5)
        d2 = date(2026, 11, 15)

        o1 = self._create_delivered_order(self.customer1, d1, [(self.chai, 10, Decimal('20.00'))])  # ₹200
        o2 = self._create_delivered_order(self.customer1, d2, [
            (self.chai, 5, Decimal('20.00')),    # ₹100
            (self.coffee, 4, Decimal('30.00'))   # ₹120
        ])  # ₹220

        # Generate for Nov 2026
        invoices = MonthlyBillingService.generate_monthly_invoices_for_month(
            year=2026,
            month=11,
            owner_user=self.owner,
            customer_ids=[self.customer1.id]
        )

        self.assertEqual(len(invoices), 1)
        inv = invoices[0]
        self.assertEqual(inv.customer_id, self.customer1.id)
        self.assertEqual(inv.billing_period, '2026-11')
        self.assertEqual(inv.billing_type, 'monthly')
        # Total should be 200 + 220 = 420
        self.assertEqual(inv.subtotal, Decimal('420.00'))
        self.assertEqual(inv.grand_total, Decimal('420.00'))
        self.assertEqual(inv.status, 'unpaid')

        # Check attached orders
        attached_order_ids = set(inv.invoice_orders.values_list('order_id', flat=True))
        self.assertIn(o1.id, attached_order_ids)
        self.assertIn(o2.id, attached_order_ids)

        # Check order.has_invoice is True
        o1.refresh_from_db()
        o2.refresh_from_db()
        self.assertTrue(o1.has_invoice)
        self.assertTrue(o2.has_invoice)

    def test_3_duplicate_generation_prevention_and_retry_safety(self):
        """3: Monthly generation must be safe to retry and prevent duplicates."""
        d1 = date(2026, 11, 10)
        self._create_delivered_order(self.customer1, d1, [(self.chai, 5, Decimal('20.00'))])

        # First run
        invoices_1 = MonthlyBillingService.generate_monthly_invoices_for_month(
            year=2026,
            month=11,
            owner_user=self.owner,
            customer_ids=[self.customer1.id]
        )
        self.assertEqual(len(invoices_1), 1)

        # Immediate retry
        invoices_2 = MonthlyBillingService.generate_monthly_invoices_for_month(
            year=2026,
            month=11,
            owner_user=self.owner,
            customer_ids=[self.customer1.id]
        )
        # Should safely skip existing, returning 0 new invoices
        self.assertEqual(len(invoices_2), 0)

        # Only one invoice in database for this customer & period
        count = Invoice.objects.filter(customer=self.customer1, billing_period='2026-11').count()
        self.assertEqual(count, 1)

    def test_4_and_7_oldest_outstanding_first_cash_allocation(self):
        """4, 6 & 7: Oldest outstanding first allocation across multiple invoices (Nov vs Dec)."""
        # Create November invoice for ₹1,000
        inv_nov = Invoice.objects.create(
            invoice_number='INV-202611-0001',
            customer=self.customer1,
            billing_period='2026-11',
            billing_type='monthly',
            subtotal=Decimal('1000.00'),
            grand_total=Decimal('1000.00'),
            status='unpaid',
            created_at=timezone.make_aware(datetime(2026, 11, 30, 23, 59))
        )

        # Create December invoice for ₹1,500
        inv_dec = Invoice.objects.create(
            invoice_number='INV-202612-0001',
            customer=self.customer1,
            billing_period='2026-12',
            billing_type='monthly',
            subtotal=Decimal('1500.00'),
            grand_total=Decimal('1500.00'),
            status='unpaid',
            created_at=timezone.make_aware(datetime(2026, 12, 31, 23, 59))
        )

        # Customer pays ₹1,200 in CASH
        response = self.client.post(f'/api/v1/billing/customers/{self.customer1.id}/collect-payment/', {
            'amount': '1200.00',
            'method': 'cash',
            'notes': 'Partial payment for Nov & Dec'
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        inv_nov.refresh_from_db()
        inv_dec.refresh_from_db()

        # November: ₹1,000 fully paid
        self.assertEqual(inv_nov.total_paid, Decimal('1000.00'))
        self.assertEqual(inv_nov.pending_amount, Decimal('0.00'))
        self.assertEqual(inv_nov.status, 'paid')

        # December: ₹200 allocated, ₹1,300 outstanding
        self.assertEqual(inv_dec.total_paid, Decimal('200.00'))
        self.assertEqual(inv_dec.pending_amount, Decimal('1300.00'))
        self.assertEqual(inv_dec.status, 'partially_paid')

        # Check total allocations created
        allocs = PaymentAllocation.objects.filter(payment__customer=self.customer1).order_by('id')
        self.assertEqual(len(allocs), 2)
        self.assertEqual(allocs[0].allocated_amount, Decimal('1000.00'))
        self.assertEqual(allocs[0].invoice_id, inv_nov.id)
        self.assertEqual(allocs[1].allocated_amount, Decimal('200.00'))
        self.assertEqual(allocs[1].invoice_id, inv_dec.id)

    def test_5_cash_only_payment_recording(self):
        """Cash-only requirement: Validates cash payment collection and FIFO allocation."""
        inv = Invoice.objects.create(
            invoice_number='INV-202611-CASH',
            customer=self.customer1,
            billing_period='2026-11',
            billing_type='monthly',
            subtotal=Decimal('500.00'),
            grand_total=Decimal('500.00'),
            status='unpaid',
        )
        response = self.client.post(f'/api/v1/billing/customers/{self.customer1.id}/collect-payment/', {
            'amount': '500.00',
            'method': 'cash',
            'notes': 'Full cash collection'
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'paid')
        self.assertEqual(inv.pending_amount, Decimal('0.00'))

    def test_8_payment_history_preservation_and_monthly_ledger(self):
        """8: Ledger overview accurately reports unpaid historical invoices and payment history."""
        # Create an unpaid invoice
        inv = Invoice.objects.create(
            invoice_number='INV-202610-0001',
            customer=self.customer1,
            billing_period='2026-10',
            billing_type='monthly',
            subtotal=Decimal('500.00'),
            grand_total=Decimal('500.00'),
            status='unpaid'
        )

        # Collect cash ₹200
        self.client.post(f'/api/v1/billing/customers/{self.customer1.id}/collect-payment/', {
            'amount': '200.00',
            'method': 'cash',
        })

        # Fetch monthly ledger overview
        response = self.client.get(f'/api/v1/billing/customers/{self.customer1.id}/monthly-ledger/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data
        self.assertEqual(data['total_outstanding'], '300.00')
        self.assertEqual(len(data['monthly_records']), 1)
        self.assertEqual(data['monthly_records'][0]['status'], 'PARTIALLY_PAID')
        self.assertEqual(data['monthly_records'][0]['remaining_balance'], '300.00')
        self.assertEqual(len(data['payment_history']), 1)
        self.assertEqual(data['payment_history'][0]['amount'], '200.00')

    def test_9_invoice_adjustment_audit_and_balance_recalculation(self):
        """9: Invoice adjustment with audit log preserves payment and recalculates balance."""
        inv = Invoice.objects.create(
            invoice_number='INV-202611-0099',
            customer=self.customer1,
            billing_period='2026-11',
            billing_type='monthly',
            subtotal=Decimal('400.00'),
            grand_total=Decimal('400.00'),
            status='unpaid'
        )
        item = InvoiceItem.objects.create(
            invoice=inv,
            product=self.chai,
            product_name='Masala Chai',
            unit_price=Decimal('20.00'),
            quantity=20,
            total_price=Decimal('400.00')
        )

        # Pay in full (₹400)
        self.client.post(f'/api/v1/billing/customers/{self.customer1.id}/collect-payment/', {
            'amount': '400.00',
            'method': 'cash',
        })
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'paid')
        self.assertEqual(inv.pending_amount, Decimal('0.00'))

        # Now adjust bill upwards (delivered quantity was 25 instead of 20 = ₹500)
        response = self.client.patch(f'/api/v1/billing/invoices/{inv.id}/edit_bill/', {
            'items': [{
                'product_id': self.chai.id,
                'product_name': 'Masala Chai',
                'quantity': 25,
                'unit_price': '20.00'
            }],
            'reason': 'Adjusted 5 additional delivered cups verified by owner'
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        inv.refresh_from_db()
        self.assertEqual(inv.grand_total, Decimal('500.00'))
        # Total paid is still ₹400; pending is now ₹100
        self.assertEqual(inv.total_paid, Decimal('400.00'))
        self.assertEqual(inv.pending_amount, Decimal('100.00'))
        # Status automatically transitions from paid to partially_paid
        self.assertEqual(inv.status, 'partially_paid')

        # Verify Audit Log
        audits = InvoiceAuditLog.objects.filter(invoice=inv)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].previous_total, Decimal('400.00'))
        self.assertEqual(audits[0].updated_total, Decimal('500.00'))
        self.assertIn('5 additional delivered cups', audits[0].reason)

    def test_10_and_11_customer_isolation(self):
        """10 & 11: Different customers' invoices remain strictly isolated."""
        d1 = date(2026, 11, 8)
        self._create_delivered_order(self.customer1, d1, [(self.chai, 10, Decimal('20.00'))])
        self._create_delivered_order(self.customer2, d1, [(self.coffee, 10, Decimal('30.00'))])

        # Generate monthly invoices for both
        invoices = MonthlyBillingService.generate_monthly_invoices_for_month(
            year=2026,
            month=11,
            owner_user=self.owner
        )
        self.assertEqual(len(invoices), 2)

        inv_c1 = Invoice.objects.get(customer=self.customer1, billing_period='2026-11')
        inv_c2 = Invoice.objects.get(customer=self.customer2, billing_period='2026-11')

        self.assertEqual(inv_c1.grand_total, Decimal('200.00'))
        self.assertEqual(inv_c2.grand_total, Decimal('300.00'))

        # Payment on Customer 1 must never allocate to Customer 2
        self.client.post(f'/api/v1/billing/customers/{self.customer1.id}/collect-payment/', {
            'amount': '200.00',
            'method': 'cash',
        })

        inv_c1.refresh_from_db()
        inv_c2.refresh_from_db()

        self.assertEqual(inv_c1.status, 'paid')
        self.assertEqual(inv_c2.status, 'unpaid')
        self.assertEqual(inv_c2.total_paid, Decimal('0.00'))

    def test_12_unauthorized_access_prevention(self):
        """12: Non-owner/non-staff cannot generate monthly bills."""
        driver_client = APIClient()
        driver_client.force_authenticate(user=self.driver_user)

        response = driver_client.post('/api/v1/billing/invoices/generate-monthly/', {
            'year': 2026,
            'month': 11,
        })
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
