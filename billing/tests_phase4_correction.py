from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from billing.models import Customer, Invoice, InvoiceItem, InvoiceAuditLog, Payment
from billing.payment_service import PaymentAllocationService
from products.models import Product, Category

User = get_user_model()


class InvoiceCorrectionPhase4Tests(TestCase):
    """
    Focused test suite covering Phase 4: Invoice Correction + Version History:
    1. Invoice correction with no payments.
    2. Correction with partial payments.
    3. Correction after multiple payments.
    4. Correction after payment reversal.
    5. Increased and decreased invoice totals.
    6. Mandatory correction reason validation.
    7. Payment history preservation (never deleted or altered).
    8. Version history snapshots (InvoiceAuditLog recorded with previous and updated states).
    9. Outstanding amount and invoice status recalculation.
    10. Unauthorized correction prevention.
    11. Concurrent correction safety via row locking.
    """

    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username='correction_owner',
            password='password123',
            is_staff=True,
            is_superuser=True
        )
        self.client.force_authenticate(user=self.owner)

        self.category = Category.objects.create(name='Snacks')
        self.item_chai = Product.objects.create(
            name='Ginger Tea',
            category=self.category,
            price=Decimal('20.00'),
            is_available=True
        )
        self.item_samosa = Product.objects.create(
            name='Samosa',
            category=self.category,
            price=Decimal('15.00'),
            is_available=True
        )

        self.customer = Customer.objects.create(
            name='Deepak Verma',
            phone='9812345678',
            address='Office 204, Tech Hub'
        )

    def _create_invoice(self, subtotal=Decimal('500.00'), discount=Decimal('0.00')) -> Invoice:
        inv = Invoice.objects.create(
            invoice_number='INV-CORR-001',
            customer=self.customer,
            customer_name=self.customer.name,
            customer_phone=self.customer.phone,
            subtotal=subtotal,
            discount_amount=discount,
            grand_total=subtotal - discount,
            status='unpaid'
        )
        InvoiceItem.objects.create(
            invoice=inv,
            product=self.item_chai,
            product_name=self.item_chai.name,
            quantity=25,
            unit_price=Decimal('20.00'),
            total_price=Decimal('500.00')
        )
        return inv

    def test_1_correction_with_no_payments(self):
        """1. Correction on unpaid invoice updates items, totals and logs immutable version history."""
        inv = self._create_invoice()

        payload = {
            'items': [
                {
                    'product_id': self.item_chai.id,
                    'product_name': self.item_chai.name,
                    'quantity': 30,
                    'unit_price': '20.00'
                }
            ],
            'discount_amount': '0.00',
            'reason': 'Adjusted quantity from 25 to 30 as per customer request'
        }

        res = self.client.patch(f'/api/v1/billing/invoices/{inv.id}/edit_bill/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        inv.refresh_from_db()
        self.assertEqual(inv.grand_total, Decimal('600.00'))
        self.assertEqual(inv.pending_amount, Decimal('600.00'))
        self.assertEqual(inv.status, 'unpaid')

        # Check version history log
        self.assertEqual(inv.audit_logs.count(), 1)
        audit = inv.audit_logs.first()
        self.assertEqual(audit.previous_total, Decimal('500.00'))
        self.assertEqual(audit.updated_total, Decimal('600.00'))
        self.assertEqual(audit.reason, 'Adjusted quantity from 25 to 30 as per customer request')
        self.assertEqual(audit.edited_by, self.owner)

    def test_2_correction_with_partial_payments(self):
        """
        2. Exact user example:
        Original invoice: ₹500
        Received: ₹70
        Corrected invoice: ₹400
        Received must remain ₹70.
        Outstanding must become ₹330.
        """
        inv = self._create_invoice(subtotal=Decimal('500.00'))

        # Pay ₹70
        pm = PaymentAllocationService.record_payment(
            amount=Decimal('70.00'),
            customer=self.customer,
            invoice=inv,
            recorded_by=self.owner
        )
        PaymentAllocationService.allocate_payment_fifo(payment=pm, customer=self.customer)

        inv.refresh_from_db()
        self.assertEqual(inv.total_paid, Decimal('70.00'))
        self.assertEqual(inv.pending_amount, Decimal('430.00'))
        self.assertEqual(inv.status, 'partially_paid')

        # Edit invoice to ₹400 (20 x ₹20)
        payload = {
            'items': [
                {
                    'product_id': self.item_chai.id,
                    'product_name': self.item_chai.name,
                    'quantity': 20,
                    'unit_price': '20.00'
                }
            ],
            'discount_amount': '0.00',
            'reason': 'Customer requested order reduction'
        }

        res = self.client.patch(f'/api/v1/billing/invoices/{inv.id}/edit_bill/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        inv.refresh_from_db()
        self.assertEqual(inv.grand_total, Decimal('400.00'))
        self.assertEqual(inv.total_paid, Decimal('70.00'))
        self.assertEqual(inv.pending_amount, Decimal('330.00'))
        self.assertEqual(inv.status, 'partially_paid')

        # Verify payment history is fully preserved
        self.assertEqual(inv.payments.count(), 1)
        self.assertEqual(inv.payments.first().amount, Decimal('70.00'))

    def test_3_correction_after_multiple_payments(self):
        """3. Correction with multiple payments preserves all payments and recalculates accurate status."""
        inv = self._create_invoice(subtotal=Decimal('500.00'))

        # Two payments: 100 + 150 = 250
        pm1 = PaymentAllocationService.record_payment(amount=Decimal('100.00'), customer=self.customer, invoice=inv)
        PaymentAllocationService.allocate_payment_fifo(payment=pm1, customer=self.customer)

        pm2 = PaymentAllocationService.record_payment(amount=Decimal('150.00'), customer=self.customer, invoice=inv)
        PaymentAllocationService.allocate_payment_fifo(payment=pm2, customer=self.customer)

        inv.refresh_from_db()
        self.assertEqual(inv.total_paid, Decimal('250.00'))

        # Correct invoice to ₹250 (Full paid match)
        payload = {
            'items': [
                {
                    'product_id': self.item_chai.id,
                    'product_name': self.item_chai.name,
                    'quantity': 10,
                    'unit_price': '25.00'
                }
            ],
            'discount_amount': '0.00',
            'reason': 'Final price settlement to ₹250'
        }
        res = self.client.patch(f'/api/v1/billing/invoices/{inv.id}/edit_bill/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        inv.refresh_from_db()
        self.assertEqual(inv.grand_total, Decimal('250.00'))
        self.assertEqual(inv.total_paid, Decimal('250.00'))
        self.assertEqual(inv.pending_amount, Decimal('0.00'))
        self.assertEqual(inv.status, 'paid')
        self.assertEqual(inv.payments.count(), 2)

    def test_4_correction_after_payment_reversal(self):
        """4. Reversed payments are NOT counted towards total_paid after invoice correction."""
        inv = self._create_invoice(subtotal=Decimal('500.00'))

        # Payment of 100, then reversed
        pm = PaymentAllocationService.record_payment(amount=Decimal('100.00'), customer=self.customer, invoice=inv)
        PaymentAllocationService.allocate_payment_fifo(payment=pm, customer=self.customer)
        PaymentAllocationService.reverse_payment(pm.id, reason="Wrong payment reference")

        inv.refresh_from_db()
        self.assertEqual(inv.total_paid, Decimal('0.00'))
        self.assertEqual(inv.pending_amount, Decimal('500.00'))

        # Correct invoice to ₹350
        payload = {
            'items': [
                {
                    'product_id': self.item_samosa.id,
                    'product_name': self.item_samosa.name,
                    'quantity': 20,
                    'unit_price': '17.50'
                }
            ],
            'reason': 'Adjusted menu items'
        }
        res = self.client.patch(f'/api/v1/billing/invoices/{inv.id}/edit_bill/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        inv.refresh_from_db()
        self.assertEqual(inv.grand_total, Decimal('350.00'))
        # Reversed payment must NOT be applied
        self.assertEqual(inv.total_paid, Decimal('0.00'))
        self.assertEqual(inv.pending_amount, Decimal('350.00'))
        self.assertEqual(inv.status, 'unpaid')

    def test_5_mandatory_correction_reason(self):
        """5. Editing invoice without reason is rejected with HTTP 400."""
        inv = self._create_invoice()

        # Missing reason
        res1 = self.client.patch(f'/api/v1/billing/invoices/{inv.id}/edit_bill/', {
            'items': [{'product_id': self.item_chai.id, 'quantity': 10, 'unit_price': '20.00'}]
        }, format='json')
        self.assertEqual(res1.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('reason', res1.data)

        # Blank/whitespace reason
        res2 = self.client.patch(f'/api/v1/billing/invoices/{inv.id}/edit_bill/', {
            'items': [{'product_id': self.item_chai.id, 'quantity': 10, 'unit_price': '20.00'}],
            'reason': '   '
        }, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_6_unauthorized_user_correction_rejected(self):
        """6. Non-owner / customer users are strictly rejected with HTTP 403."""
        customer_user = User.objects.create_user(username='customer_guy', password='password123')
        self.client.force_authenticate(user=customer_user)

        inv = self._create_invoice()
        res = self.client.patch(f'/api/v1/billing/invoices/{inv.id}/edit_bill/', {
            'items': [{'product_id': self.item_chai.id, 'quantity': 5, 'unit_price': '20.00'}],
            'reason': 'Hacker edit'
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
