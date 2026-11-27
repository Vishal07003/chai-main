from decimal import Decimal
from datetime import date
from io import BytesIO
import openpyxl
from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from accounts.models import UserProfile
from billing.models import (
    Customer,
    Invoice,
    InvoiceItem,
    Payment,
    PaymentAllocation,
    CustomerCreditTransaction,
)
from billing.customer_credit_service import CustomerCreditService
from billing.ledger_service import LedgerService
from products.models import Product, Category


class Phase567CombinedTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        # 1. Business Owner
        self.owner = User.objects.create_user(username='chai_boss', password='Password123!', is_staff=True)
        self.owner_profile = UserProfile.objects.create(user=self.owner, phone='9876543210', role='owner')

        # 2. Regular User
        self.regular_user = User.objects.create_user(username='regular_staff', password='Password123!')
        self.regular_profile = UserProfile.objects.create(user=self.regular_user, phone='9123456789', role='delivery')

        # 3. Customer
        self.customer = Customer.objects.create(name='Tech Park Cafe', phone='9888877777', address='Building A')

        # 4. Product
        cat = Category.objects.create(name='Tea')
        self.product = Product.objects.create(name='Masala Chai Flask', price=Decimal('150.00'), category=cat)

    def test_phase5_credit_balance_creation_and_usage(self):
        """Phase 5: Excess payment designated as Credit Balance and applied to future invoice."""
        # Invoice 1: ₹500, paid ₹500 in full
        inv1 = Invoice.objects.create(
            customer=self.customer,
            invoice_number='INV-001',
            subtotal=Decimal('500.00'),
            grand_total=Decimal('500.00'),
        )
        pm1 = Payment.objects.create(
            amount=Decimal('500.00'),
            method='upi',
            customer=self.customer,
            invoice=inv1,
            recorded_by=self.owner,
        )
        PaymentAllocation.objects.create(payment=pm1, invoice=inv1, allocated_amount=Decimal('500.00'))
        inv1.update_payment_status()
        self.assertEqual(inv1.status, 'paid')

        # Owner corrects invoice 1 down to ₹350 (excess = ₹150)
        self.client.force_authenticate(user=self.owner)
        edit_payload = {
            'reason': 'Customer returned 1 flask due to early meeting end',
            'discount_amount': '150.00',  # Grand total becomes ₹350
            'excess_treatment': 'credit',
        }
        res = self.client.patch(f'/api/v1/billing/invoices/{inv1.id}/edit_bill/', edit_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # Verify Credit Balance
        balances = CustomerCreditService.get_balances(customer=self.customer)
        self.assertEqual(balances['available_credit'], Decimal('150.00'))
        self.assertEqual(balances['refund_due'], Decimal('0.00'))

        # Create Invoice 2: ₹200
        inv2 = Invoice.objects.create(
            customer=self.customer,
            invoice_number='INV-002',
            subtotal=Decimal('200.00'),
            grand_total=Decimal('200.00'),
            status='unpaid'
        )
        # Apply ₹150 credit to Invoice 2
        tx = CustomerCreditService.apply_credit_to_invoice(
            invoice=inv2,
            amount=Decimal('150.00'),
            applied_by=self.owner,
        )
        self.assertEqual(tx.transaction_type, 'CREDIT_APPLIED')
        inv2.refresh_from_db()
        self.assertEqual(inv2.pending_amount, Decimal('50.00'))
        self.assertEqual(inv2.status, 'partially_paid')

        # Remaining credit is now ₹0
        rem_balances = CustomerCreditService.get_balances(customer=self.customer)
        self.assertEqual(rem_balances['available_credit'], Decimal('0.00'))

    def test_phase5_refund_due_creation_and_manual_settlement(self):
        """Phase 5: Excess payment designated as Refund Due and manual settlement recorded."""
        inv = Invoice.objects.create(
            customer=self.customer,
            invoice_number='INV-REF-001',
            subtotal=Decimal('600.00'),
            grand_total=Decimal('600.00'),
        )
        pm = Payment.objects.create(
            amount=Decimal('600.00'),
            method='cash',
            customer=self.customer,
            invoice=inv,
            recorded_by=self.owner,
        )
        PaymentAllocation.objects.create(payment=pm, invoice=inv, allocated_amount=Decimal('600.00'))
        inv.update_payment_status()

        # Correct invoice down to ₹400 with 'refund' excess treatment
        self.client.force_authenticate(user=self.owner)
        edit_payload = {
            'reason': 'Order quantity miscalculated by counter staff',
            'discount_amount': '200.00',
            'excess_treatment': 'refund',
        }
        res = self.client.patch(f'/api/v1/billing/invoices/{inv.id}/edit_bill/', edit_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        balances = CustomerCreditService.get_balances(customer=self.customer)
        self.assertEqual(balances['refund_due'], Decimal('200.00'))

        # Manual settlement
        settle_tx = CustomerCreditService.settle_refund(
            customer=self.customer,
            amount=Decimal('200.00'),
            settled_by=self.owner,
            reason='Paid ₹200 back in cash at stall'
        )
        self.assertEqual(settle_tx.transaction_type, 'REFUND_SETTLED')
        rem_balances = CustomerCreditService.get_balances(customer=self.customer)
        self.assertEqual(rem_balances['refund_due'], Decimal('0.00'))

    def test_phase6_reversed_payment_exclusion_from_ledger_and_reconciliation(self):
        """Phase 6: Reversed payments are excluded from valid collections and ledger balances."""
        inv = Invoice.objects.create(
            customer=self.customer,
            invoice_number='INV-REV-001',
            subtotal=Decimal('1000.00'),
            grand_total=Decimal('1000.00'),
        )
        pm = Payment.objects.create(
            amount=Decimal('400.00'),
            method='upi',
            customer=self.customer,
            invoice=inv,
            recorded_by=self.owner,
        )
        PaymentAllocation.objects.create(payment=pm, invoice=inv, allocated_amount=Decimal('400.00'))
        inv.update_payment_status()
        self.assertEqual(inv.total_paid, Decimal('400.00'))
        self.assertEqual(inv.pending_amount, Decimal('600.00'))

        # Reverse payment
        self.client.force_authenticate(user=self.owner)
        rev_res = self.client.post(f'/api/v1/billing/payments/{pm.id}/reverse/', {'reason': 'Customer UPI failed'}, format='json')
        self.assertEqual(rev_res.status_code, status.HTTP_200_OK)

        inv.refresh_from_db()
        self.assertEqual(inv.total_paid, Decimal('0.00'))
        self.assertEqual(inv.pending_amount, Decimal('1000.00'))

        # Ledger overview check
        overview = LedgerService.get_customer_monthly_ledger_overview(self.customer)
        p_record = next(p for p in overview['payment_history'] if p['payment_id'] == pm.id)
        self.assertTrue(p_record['is_reversed'])
        self.assertEqual(p_record['reversal_reason'], 'Customer UPI failed')

    def test_phase7_excel_invoice_endpoint_200_and_no_406(self):
        """Phase 7: Invoice Excel endpoint returns 200 OK with correct binary MIME type, fixing 406."""
        inv = Invoice.objects.create(
            customer=self.customer,
            invoice_number='INV-EXCEL-001',
            subtotal=Decimal('300.00'),
            grand_total=Decimal('300.00'),
        )
        InvoiceItem.objects.create(
            invoice=inv,
            product=self.product,
            product_name='Masala Chai Flask',
            unit_price=Decimal('150.00'),
            quantity=2,
            total_price=Decimal('300.00'),
        )

        self.client.force_authenticate(user=self.owner)

        # Test request with standard React Native Accept header
        res = self.client.get(
            f'/api/v1/billing/invoices/{inv.id}/excel/',
            HTTP_ACCEPT='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertIn('.xlsx', res['Content-Disposition'])

        # Verify it is a valid XLSX workbook
        wb = openpyxl.load_workbook(BytesIO(res.content))
        self.assertGreater(len(wb.sheetnames), 0)

    def test_phase7_customer_ledger_pdf_and_excel_exports(self):
        """Phase 7: Customer Ledger PDF and Excel endpoints generate valid files with reconciled balances."""
        inv = Invoice.objects.create(
            customer=self.customer,
            invoice_number='INV-LEDGER-001',
            subtotal=Decimal('450.00'),
            grand_total=Decimal('450.00'),
        )
        InvoiceItem.objects.create(
            invoice=inv,
            product=self.product,
            product_name='Masala Chai Flask',
            unit_price=Decimal('150.00'),
            quantity=3,
            total_price=Decimal('450.00'),
        )

        self.client.force_authenticate(user=self.owner)

        # 1. Customer Ledger PDF
        pdf_res = self.client.get(f'/api/v1/billing/customers/{self.customer.id}/ledger-pdf/')
        self.assertEqual(pdf_res.status_code, status.HTTP_200_OK)
        self.assertEqual(pdf_res['Content-Type'], 'application/pdf')
        self.assertTrue(pdf_res.content.startswith(b'%PDF'))

        # 2. Customer Ledger Excel
        excel_res = self.client.get(f'/api/v1/billing/customers/{self.customer.id}/ledger-excel/')
        self.assertEqual(excel_res.status_code, status.HTTP_200_OK)
        self.assertEqual(excel_res['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

        wb = openpyxl.load_workbook(BytesIO(excel_res.content))
        self.assertIn("Ledger Summary", wb.sheetnames)
        self.assertIn("Invoices", wb.sheetnames)
        self.assertIn("Payments & Collections", wb.sheetnames)
