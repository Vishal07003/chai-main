from django.test import TestCase
from django.contrib.auth.models import User
from decimal import Decimal
import datetime
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import UserProfile
from products.models import Product, Category
from billing.models import (
    Customer,
    Invoice,
    InvoiceItem,
    InvoiceOrder,
    DraftInvoice,
    DraftInvoiceItem,
    Payment,
    PaymentAllocation,
    Expense,
)
from orders.models import Order, OrderItem
from deliveries.models import Delivery, DeliveryPartner, DeliveryItem


class ReportsBackendEngineTests(TestCase):
    def setUp(self):
        # 1. Users
        self.owner = User.objects.create_user(username='report_owner', password='password123', email='rep_owner@chaiflow.com')
        self.owner_profile = UserProfile.objects.create(user=self.owner, phone='9888877771', role='owner')

        self.staff = User.objects.create_user(username='delivery_staff', password='password123', email='rep_staff@chaiflow.com')
        self.staff_profile = UserProfile.objects.create(user=self.staff, phone='9888877772', role='delivery')

        # 2. Master Data
        self.category = Category.objects.create(name='Chai Specialties')
        self.product_elaichi = Product.objects.create(
            name='Special Elaichi Chai',
            category=self.category,
            price=Decimal('30.00'),
            cost_price=Decimal('12.00'),
            unit='Cup',
        )
        self.product_bun_maska = Product.objects.create(
            name='Bun Maska',
            category=self.category,
            price=Decimal('40.00'),
            cost_price=Decimal('18.00'),
            unit='Plate',
        )

        self.customer1 = Customer.objects.create(name='Aarav Mehta', phone='9820011223', address='Tower A, Tech Park')
        self.customer2 = Customer.objects.create(name='Priya Sharma', phone='9820044556', address='Tower B, Tech Park')

        self.client = APIClient()

    def test_owner_permissions_enforced_across_all_reports(self):
        """Non-owners (staff, delivery, unauthenticated) are blocked from all reporting endpoints."""
        endpoints = [
            '/api/v1/reports/overview/',
            '/api/v1/reports/sales/',
            '/api/v1/reports/collections/',
            '/api/v1/reports/outstanding/',
            '/api/v1/reports/products/',
            '/api/v1/reports/customers/',
            '/api/v1/reports/deliveries/',
            '/api/v1/reports/discounts/',
            '/api/v1/reports/profit-loss/',
            '/api/v1/reports/export/pdf/?report_type=sales',
            '/api/v1/reports/export/excel/?report_type=sales',
        ]

        # Unauthenticated -> 401
        for ep in endpoints:
            resp = self.client.get(ep)
            self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED, f"Failed on unauth {ep}")

        # Delivery staff -> 403
        self.client.force_authenticate(user=self.staff)
        for ep in endpoints:
            resp = self.client.get(ep)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, f"Failed on forbidden {ep}")

    def test_sales_report_draft_exclusion_and_billing_breakdown(self):
        """Finalized non-void invoices are counted. Drafts and void invoices are strictly excluded."""
        self.client.force_authenticate(user=self.owner)

        # 1. Draft invoice (must NOT be counted)
        draft = DraftInvoice.objects.create(
            draft_number='DRAFT-TEST-001',
            customer=self.customer1,
            customer_name=self.customer1.name,
            subtotal=Decimal('300.00'),
            grand_total=Decimal('300.00'),
            status='draft'
        )

        # 2. Finalized Quick Billing Invoice
        inv_qb = Invoice.objects.create(
            invoice_number='INV-QB-001',
            customer=self.customer1,
            customer_name=self.customer1.name,
            subtotal=Decimal('100.00'),
            discount_amount=Decimal('10.00'),
            grand_total=Decimal('90.00'),
            status='paid',
        )
        InvoiceItem.objects.create(
            invoice=inv_qb,
            product=self.product_elaichi,
            product_name=self.product_elaichi.name,
            unit_price=Decimal('30.00'),
            quantity=3,
            total_price=Decimal('90.00'),
            unit_cost=Decimal('12.00'),
            total_cost=Decimal('36.00')
        )

        # 3. Void Invoice (must NOT be counted)
        Invoice.objects.create(
            invoice_number='INV-VOID-001',
            customer=self.customer1,
            customer_name=self.customer1.name,
            subtotal=Decimal('200.00'),
            grand_total=Decimal('200.00'),
            status='void',
        )

        resp = self.client.get('/api/v1/reports/sales/?filter_type=today')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data

        # Verify only INV-QB-001 is included
        self.assertEqual(data['summary']['invoice_count'], 1)
        self.assertEqual(Decimal(data['summary']['net_sales']), Decimal('90.00'))
        self.assertEqual(Decimal(data['summary']['total_discounts']), Decimal('10.00'))
        self.assertEqual(Decimal(data['summary']['quick_billing']['sales']), Decimal('90.00'))

    def test_collections_advances_and_zero_double_counting(self):
        """Collections accurately reflect Payment.received_at, including advances, without double counting."""
        self.client.force_authenticate(user=self.owner)

        # Record payment with advance (excess amount unallocated)
        inv = Invoice.objects.create(
            invoice_number='INV-COLL-001',
            customer=self.customer1,
            customer_name=self.customer1.name,
            subtotal=Decimal('100.00'),
            grand_total=Decimal('100.00'),
            status='unpaid',
        )
        payment = Payment.objects.create(
            customer=self.customer1,
            amount=Decimal('150.00'),
            method='upi',
            reference_number='UPI123456',
        )
        # Allocate 100 to invoice, 50 remains advance
        PaymentAllocation.objects.create(
            payment=payment,
            invoice=inv,
            allocated_amount=Decimal('100.00'),
        )
        inv.update_payment_status()

        resp = self.client.get('/api/v1/reports/collections/?filter_type=today')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        summary = resp.data['summary']

        self.assertEqual(Decimal(summary['total_collections']), Decimal('150.00'))
        self.assertEqual(summary['payment_count'], 1)
        self.assertEqual(Decimal(summary['methods']['upi']['total']), Decimal('150.00'))

    def test_outstanding_overdue_due_date_rules(self):
        """Only invoices with explicit past due_date and positive pending_amount count as overdue."""
        self.client.force_authenticate(user=self.owner)
        today = datetime.date.today()

        # 1. Invoice with past due date -> Overdue
        Invoice.objects.create(
            invoice_number='INV-DUE-PAST',
            customer=self.customer1,
            customer_name=self.customer1.name,
            grand_total=Decimal('100.00'),
            due_date=today - datetime.timedelta(days=3),
            status='unpaid',
        )

        # 2. Invoice with future due date -> NOT Overdue
        Invoice.objects.create(
            invoice_number='INV-DUE-FUTURE',
            customer=self.customer1,
            customer_name=self.customer1.name,
            grand_total=Decimal('200.00'),
            due_date=today + datetime.timedelta(days=5),
            status='unpaid',
        )

        # 3. Invoice with NULL due date -> NEVER Overdue
        Invoice.objects.create(
            invoice_number='INV-DUE-NULL',
            customer=self.customer2,
            customer_name=self.customer2.name,
            grand_total=Decimal('300.00'),
            due_date=None,
            status='unpaid',
        )

        resp = self.client.get('/api/v1/reports/outstanding/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        summary = resp.data['summary']

        self.assertEqual(Decimal(summary['total_outstanding']), Decimal('600.00'))
        self.assertEqual(Decimal(summary['overdue_outstanding']), Decimal('100.00'))
        self.assertEqual(summary['overdue_invoices_count'], 1)

    def test_profit_loss_cost_coverage_and_expense_integration(self):
        """P&L combines Net Sales, COGS from item snapshots, and operating expenses with coverage indicators."""
        self.client.force_authenticate(user=self.owner)

        # 1. Finalized Invoice with snapshot cost
        inv = Invoice.objects.create(
            invoice_number='INV-PNL-001',
            customer=self.customer1,
            customer_name=self.customer1.name,
            subtotal=Decimal('200.00'),
            grand_total=Decimal('200.00'),
            status='paid',
        )
        InvoiceItem.objects.create(
            invoice=inv,
            product=self.product_elaichi,
            product_name=self.product_elaichi.name,
            unit_price=Decimal('30.00'),
            quantity=4,
            total_price=Decimal('120.00'),
            unit_cost=Decimal('12.00'),
            total_cost=Decimal('48.00'),
        )
        InvoiceItem.objects.create(
            invoice=inv,
            product=self.product_bun_maska,
            product_name=self.product_bun_maska.name,
            unit_price=Decimal('40.00'),
            quantity=2,
            total_price=Decimal('80.00'),
            unit_cost=Decimal('18.00'),
            total_cost=Decimal('36.00'),
        )

        # 2. Operating Expense
        Expense.objects.create(
            title='Daily Milk Supply Fuel',
            category='transport',
            amount=Decimal('40.00'),
            expense_date=datetime.date.today(),
            recorded_by=self.owner,
        )

        resp = self.client.get('/api/v1/reports/profit-loss/?filter_type=today')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        summary = resp.data['summary']

        # Net Sales = 200.00
        # COGS = 48 + 36 = 84.00
        # Gross Profit = 200 - 84 = 116.00
        # Operating Expenses = 40.00
        # Net Profit = 116 - 40 = 76.00
        # Margin = 76 / 200 = 38.0%
        self.assertEqual(Decimal(summary['net_sales']), Decimal('200.00'))
        self.assertEqual(Decimal(summary['cogs']), Decimal('84.00'))
        self.assertEqual(summary['cost_coverage_status'], 'complete')
        self.assertEqual(Decimal(summary['gross_profit']), Decimal('116.00'))
        self.assertEqual(Decimal(summary['operating_expenses']), Decimal('40.00'))
        self.assertEqual(Decimal(summary['net_profit']), Decimal('76.00'))
        self.assertEqual(summary['profit_margin_pct'], 38.0)

    def test_pdf_and_excel_exports(self):
        """PDF and Excel exports return valid binary payloads with appropriate headers."""
        self.client.force_authenticate(user=self.owner)

        # Test PDF export with standard Accept header
        resp_pdf = self.client.get('/api/v1/reports/export/pdf/?report_type=sales&filter_type=today')
        self.assertEqual(resp_pdf.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_pdf['Content-Type'], 'application/pdf')
        self.assertTrue(resp_pdf.content.startswith(b'%PDF'))
        self.assertTrue(len(resp_pdf.content) > 1000)

        # Test PDF export with application/json Accept header (verifies ForgivingContentNegotiation prevents 406)
        resp_pdf_json_accept = self.client.get(
            '/api/v1/reports/export/pdf/?report_type=sales&filter_type=today',
            HTTP_ACCEPT='application/json, text/plain, */*'
        )
        self.assertEqual(resp_pdf_json_accept.status_code, status.HTTP_200_OK)
        self.assertTrue(resp_pdf_json_accept.content.startswith(b'%PDF'))

        # Test Excel export with standard Accept header
        resp_excel = self.client.get('/api/v1/reports/export/excel/?report_type=sales&filter_type=today')
        self.assertEqual(resp_excel.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_excel['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertTrue(resp_excel.content.startswith(b'PK'))
        self.assertTrue(len(resp_excel.content) > 1000)

        # Test Excel export with application/json Accept header
        resp_excel_json_accept = self.client.get(
            '/api/v1/reports/export/excel/?report_type=sales&filter_type=today',
            HTTP_ACCEPT='application/json'
        )
        self.assertEqual(resp_excel_json_accept.status_code, status.HTTP_200_OK)
        self.assertTrue(resp_excel_json_accept.content.startswith(b'PK'))

    def test_unified_report_and_exports(self):
        """Phase R5: Verify unified report endpoint, calculations, and PDF/Excel exports."""
        self.client.force_authenticate(user=self.owner)

        resp = self.client.get('/api/v1/reports/unified/?filter_type=last_30_days')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data

        self.assertIn('summary', data)
        self.assertIn('sales_invoices', data)
        self.assertIn('collection_transactions', data)
        self.assertIn('customer_outstanding', data)
        self.assertIn('customer_payment_history', data)
        self.assertIn('discounts', data)
        self.assertIn('expenses', data)
        self.assertIn('deliveries', data)
        self.assertIn('profit_loss', data)

        # PDF export of unified report
        resp_pdf = self.client.get('/api/v1/reports/export/pdf/?report_type=unified&filter_type=last_30_days')
        self.assertEqual(resp_pdf.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_pdf['Content-Type'], 'application/pdf')
        self.assertTrue(resp_pdf.content.startswith(b'%PDF'))
        self.assertIn('ChaiFlow_Business_Report_Last_30_Days.pdf', resp_pdf['Content-Disposition'])

        # Excel export of unified report
        resp_excel = self.client.get('/api/v1/reports/export/excel/?report_type=unified&filter_type=last_30_days')
        self.assertEqual(resp_excel.status_code, status.HTTP_200_OK)
        self.assertTrue(resp_excel.content.startswith(b'PK'))
        self.assertIn('ChaiFlow_Business_Report_Last_30_Days.xlsx', resp_excel['Content-Disposition'])
