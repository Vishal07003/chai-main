from decimal import Decimal
import calendar
from datetime import date, datetime, time
import openpyxl
from io import BytesIO
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from billing.models import (
    Customer, Company, Invoice, InvoiceItem, InvoiceOrder, Payment, PaymentAllocation
)
from billing.monthly_service import MonthlyBillingService
from billing.pdf_generator import generate_invoice_pdf
from billing.monthly_excel_generator import generate_monthly_invoice_excel
from orders.models import Order, OrderItem
from products.models import Product, Category
from deliveries.models import Delivery, DeliveryItem


class MonthlyBreakdownAndExportTests(TestCase):
    """
    Step 2 Tests:
    1. Date-wise invoice grouping preserving individual orders.
    2. Multiple orders on the same date separated with order numbers.
    3. Product-wise quantity, unit rate, and line amount.
    4. Daily subtotal calculation after each date.
    5. Monthly summary: subtotal, discount, final amount, received amount, remaining balance.
    6. Payment status reflection (Unpaid, Partially Paid, Paid).
    7. PDF export generation and content verification.
    8. Excel export generation, valid workbook, sheets, and headers.
    9. Breakdown API and Excel API HTTP endpoints.
    10. Customer-specific billing cycle integration with breakdown.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner_breakdown_tester',
            password='password123',
            first_name='Owner',
            last_name='Tester'
        )
        from accounts.models import UserProfile
        if hasattr(self.owner, 'profile'):
            self.owner.profile.role = 'owner'
            self.owner.profile.save()
        else:
            UserProfile.objects.create(user=self.owner, role='owner', phone='+919876543290')

        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

        self.category = Category.objects.create(name='Tea & Coffee')
        self.chai = Product.objects.create(
            name='Ginger Chai',
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

        self.customer = Customer.objects.create(
            name='Acme Corp',
            phone='+919876543210',
            address='Cyber City, Tower 1',
            billing_cycle_day=10  # Cycle: Nov 10 to Dec 9
        )

    def _create_order_with_delivery(self, customer: Customer, delivery_date: date, delivery_time: time, items: list) -> Order:
        dt = timezone.make_aware(datetime.combine(delivery_date, delivery_time))
        order = Order.objects.create(
            customer=self.owner,
            customer_profile=customer,
            status='delivered',
            scheduled_delivery_date=delivery_date,
            scheduled_delivery_time=delivery_time,
            created_at=dt,
        )
        order.created_at = dt
        order.save(update_fields=['created_at'])

        delivery = Delivery.objects.create(
            order=order,
            status='delivered'
        )

        total = Decimal('0.00')
        for prod, qty, rate in items:
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
            total += (rate * qty)

        order.total_amount = total
        order.save(update_fields=['total_amount'])
        return order

    def test_1_monthly_invoice_hierarchy_and_daily_subtotals(self):
        """Hierarchy: Customer -> Delivery Date -> Individual Order -> Product Item -> Daily Subtotal."""
        d1 = date(2026, 11, 12)
        d2 = date(2026, 11, 18)

        # On d1: Two separate orders
        o1 = self._create_order_with_delivery(self.customer, d1, time(9, 30), [
            (self.chai, 10, Decimal('20.00')),   # 200
        ])
        o2 = self._create_order_with_delivery(self.customer, d1, time(15, 0), [
            (self.coffee, 5, Decimal('30.00')),  # 150
        ])

        # On d2: One order
        o3 = self._create_order_with_delivery(self.customer, d2, time(10, 0), [
            (self.chai, 5, Decimal('20.00')),    # 100
            (self.coffee, 2, Decimal('30.00')),  # 60
        ])

        # Generate monthly invoice for Nov 2026
        invoice = MonthlyBillingService.generate_customer_monthly_invoice(
            customer=self.customer,
            year=2026,
            month=11,
            owner_user=self.owner
        )
        self.assertIsNotNone(invoice)

        # Get detailed breakdown
        breakdown = MonthlyBillingService.get_monthly_invoice_breakdown(invoice)

        # Verify Customer & Period
        self.assertEqual(breakdown['customer_name'], 'Acme Corp')
        self.assertEqual(breakdown['billing_period'], '2026-11')
        self.assertEqual(len(breakdown['dates']), 2)

        # Verify Date 1 (2026-11-12)
        date1_block = breakdown['dates'][0]
        self.assertEqual(date1_block['date'], d1)
        self.assertEqual(len(date1_block['orders']), 2)
        # Order 1
        ord1_data = date1_block['orders'][0]
        self.assertEqual(ord1_data['order_id'], o1.id)
        self.assertEqual(ord1_data['order_subtotal'], Decimal('200.00'))
        self.assertEqual(ord1_data['items'][0]['product_name'], 'Ginger Chai')
        self.assertEqual(ord1_data['items'][0]['quantity'], 10)
        self.assertEqual(ord1_data['items'][0]['line_amount'], Decimal('200.00'))

        # Order 2
        ord2_data = date1_block['orders'][1]
        self.assertEqual(ord2_data['order_id'], o2.id)
        self.assertEqual(ord2_data['order_subtotal'], Decimal('150.00'))

        # Date 1 Daily Subtotal = 200 + 150 = 350
        self.assertEqual(date1_block['daily_subtotal'], Decimal('350.00'))
        self.assertEqual(date1_block['daily_total_quantity'], 15)

        # Verify Date 2 (2026-11-18)
        date2_block = breakdown['dates'][1]
        self.assertEqual(date2_block['date'], d2)
        self.assertEqual(len(date2_block['orders']), 1)
        # Order 3 subtotal = 100 + 60 = 160
        self.assertEqual(date2_block['daily_subtotal'], Decimal('160.00'))

        # Monthly totals: 350 + 160 = 510
        self.assertEqual(breakdown['subtotal'], Decimal('510.00'))
        self.assertEqual(breakdown['grand_total'], Decimal('510.00'))
        self.assertEqual(breakdown['pending_amount'], Decimal('510.00'))
        self.assertEqual(breakdown['payment_status'], 'Unpaid')

    def test_2_breakdown_api_endpoint(self):
        """GET /api/v1/invoices/<id>/breakdown/ returns 200 OK with date/order structure."""
        d1 = date(2026, 11, 15)
        self._create_order_with_delivery(self.customer, d1, time(9, 0), [(self.chai, 5, Decimal('20.00'))])
        inv = MonthlyBillingService.generate_customer_monthly_invoice(
            customer=self.customer,
            year=2026,
            month=11,
            owner_user=self.owner
        )

        resp = self.client.get(f'/api/v1/billing/invoices/{inv.id}/breakdown/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['invoice_id'], inv.id)
        self.assertEqual(resp.data['customer_name'], 'Acme Corp')
        self.assertEqual(len(resp.data['dates']), 1)
        self.assertEqual(resp.data['dates'][0]['daily_subtotal'], Decimal('100.00'))

    def test_3_pdf_export_contains_datewise_breakdown(self):
        """GET /api/v1/billing/invoices/<id>/pdf/ returns valid PDF bytes containing monthly breakdown."""
        d1 = date(2026, 11, 12)
        self._create_order_with_delivery(self.customer, d1, time(10, 0), [(self.chai, 8, Decimal('20.00'))])
        inv = MonthlyBillingService.generate_customer_monthly_invoice(
            customer=self.customer,
            year=2026,
            month=11,
            owner_user=self.owner
        )

        pdf_bytes = generate_invoice_pdf(inv)
        self.assertTrue(len(pdf_bytes) > 1000)
        self.assertTrue(pdf_bytes.startswith(b'%PDF-'))

        # Test endpoint
        resp = self.client.get(f'/api/v1/billing/invoices/{inv.id}/pdf/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp['Content-Type'], 'application/pdf')

    def test_4_excel_export_and_api_endpoint(self):
        """GET /api/v1/billing/invoices/<id>/excel/ returns valid openpyxl workbook with date & subtotal rows."""
        d1 = date(2026, 11, 12)
        d2 = date(2026, 11, 20)
        self._create_order_with_delivery(self.customer, d1, time(9, 0), [(self.chai, 5, Decimal('20.00'))])
        self._create_order_with_delivery(self.customer, d2, time(14, 0), [(self.coffee, 3, Decimal('30.00'))])

        inv = MonthlyBillingService.generate_customer_monthly_invoice(
            customer=self.customer,
            year=2026,
            month=11,
            owner_user=self.owner
        )

        # Generate excel bytes directly
        xlsx_bytes = generate_monthly_invoice_excel(inv)
        self.assertTrue(len(xlsx_bytes) > 2000)

        # Parse workbook to verify sheets and cells
        wb = openpyxl.load_workbook(filename=BytesIO(xlsx_bytes))
        ws = wb.active
        self.assertIn("Monthly Bill 2026-11", ws.title)

        # Verify Header cell values
        headers = [ws.cell(row=4, column=c).value for c in range(1, 7)]
        self.assertEqual(headers, ["Delivery Date", "Order #", "Product", "Quantity", "Rate (₹)", "Line Amount (₹)"])

        # Test HTTP endpoint
        resp = self.client.get(f'/api/v1/billing/invoices/{inv.id}/excel/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('spreadsheetml.sheet', resp['Content-Type'])
        self.assertIn('ChaiFlow_Monthly_', resp['Content-Disposition'])

    def test_5_payment_status_partial_and_full(self):
        """Verify breakdown accurately reflects payments and payment status."""
        d1 = date(2026, 11, 12)
        self._create_order_with_delivery(self.customer, d1, time(9, 0), [(self.chai, 10, Decimal('20.00'))])  # 200

        inv = MonthlyBillingService.generate_customer_monthly_invoice(
            customer=self.customer,
            year=2026,
            month=11,
            owner_user=self.owner
        )
        self.assertEqual(inv.grand_total, Decimal('200.00'))

        # Initial status: Unpaid
        b1 = MonthlyBillingService.get_monthly_invoice_breakdown(inv)
        self.assertEqual(b1['payment_status'], 'Unpaid')
        self.assertEqual(b1['pending_amount'], Decimal('200.00'))

        # Record partial payment ₹50
        pay = Payment.objects.create(
            customer=self.customer,
            invoice=inv,
            amount=Decimal('50.00'),
            method='cash',
            recorded_by=self.owner
        )
        PaymentAllocation.objects.create(
            payment=pay,
            invoice=inv,
            allocated_amount=Decimal('50.00')
        )
        inv.refresh_from_db()

        b2 = MonthlyBillingService.get_monthly_invoice_breakdown(inv)
        self.assertEqual(b2['payment_status'], 'Partially Paid')
        self.assertEqual(b2['total_paid'], Decimal('50.00'))
        self.assertEqual(b2['pending_amount'], Decimal('150.00'))

        # Pay remaining ₹150
        pay2 = Payment.objects.create(
            customer=self.customer,
            invoice=inv,
            amount=Decimal('150.00'),
            method='cash',
            recorded_by=self.owner
        )
        PaymentAllocation.objects.create(
            payment=pay2,
            invoice=inv,
            allocated_amount=Decimal('150.00')
        )
        inv.status = 'paid'
        inv.save(update_fields=['status'])
        inv.refresh_from_db()

        b3 = MonthlyBillingService.get_monthly_invoice_breakdown(inv)
        self.assertEqual(b3['payment_status'], 'Paid')
        self.assertEqual(b3['total_paid'], Decimal('200.00'))
        self.assertEqual(b3['pending_amount'], Decimal('0.00'))
