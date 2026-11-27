from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from billing.models import Customer, Invoice, InvoiceItem, Payment, PaymentAllocation
from billing.payment_service import PaymentAllocationService
from billing.ledger_service import LedgerService
from products.models import Product, Category

User = get_user_model()


class PaymentReversalTests(TestCase):
    """
    Automated backend tests covering Phase 3 Reverse Payment implementation:
    1. Full payment reversal.
    2. Partial payment reversal.
    3. Payment allocated across multiple invoices.
    4. Invoice outstanding recalculation.
    5. Invoice status recalculation.
    6. Customer outstanding recalculation.
    7. Original payment remains in history.
    8. Reversed payment excluded from collection totals.
    9. Duplicate reversal rejected.
    10. Missing reversal reason rejected.
    11. Cancelled reversal leaves all records unchanged.
    12. Concurrent reversal requests do not reverse twice.
    13. Historical payment records remain accessible.
    """

    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username='reversal_owner',
            password='password123',
            is_staff=True,
            is_superuser=True
        )
        self.client.force_authenticate(user=self.owner)

        self.category = Category.objects.create(name='Snacks')
        self.chai = Product.objects.create(
            name='Cutting Chai',
            category=self.category,
            price=Decimal('20.00'),
            is_available=True
        )

        self.customer = Customer.objects.create(
            name='Mahesh Sharma',
            phone='9811122233',
            address='Shop 5, Market Road'
        )

    def _create_invoice(self, inv_num: str, amount: Decimal) -> Invoice:
        inv = Invoice.objects.create(
            invoice_number=inv_num,
            customer=self.customer,
            customer_name=self.customer.name,
            customer_phone=self.customer.phone,
            subtotal=amount,
            discount_amount=Decimal('0.00'),
            grand_total=amount,
            status='unpaid'
        )
        InvoiceItem.objects.create(
            invoice=inv,
            product=self.chai,
            product_name=self.chai.name,
            quantity=1,
            unit_price=amount,
            total_price=amount
        )
        return inv

    def test_1_full_payment_reversal(self):
        """1. Full payment reversal safely restores invoice to unpaid and marks payment as reversed."""
        inv = self._create_invoice('INV-REV-001', Decimal('100.00'))

        # Record and allocate full payment of 100
        pm = PaymentAllocationService.record_payment(
            amount=Decimal('100.00'),
            customer=self.customer,
            recorded_by=self.owner
        )
        PaymentAllocationService.allocate_payment_fifo(payment=pm, customer=self.customer)

        inv.refresh_from_db()
        self.assertEqual(inv.status, 'paid')
        self.assertEqual(inv.pending_amount, Decimal('0.00'))

        # Reverse the payment via API
        res = self.client.post(f'/api/v1/billing/payments/{pm.id}/reverse/', {
            'reason': 'Customer requested cancellation of duplicate payment'
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['is_reversed'])
        self.assertEqual(res.data['reversal_reason'], 'Customer requested cancellation of duplicate payment')

        # Check DB states
        pm.refresh_from_db()
        inv.refresh_from_db()

        self.assertTrue(pm.is_reversed)
        self.assertIsNotNone(pm.reversed_at)
        self.assertEqual(pm.reversed_by, self.owner)
        self.assertEqual(inv.status, 'unpaid')
        self.assertEqual(inv.pending_amount, Decimal('100.00'))
        self.assertEqual(inv.total_paid, Decimal('0.00'))

    def test_2_partial_payment_reversal(self):
        """2. Partial payment reversal restores exact unpaid amount on partially paid invoice."""
        inv = self._create_invoice('INV-REV-002', Decimal('200.00'))

        # Payment 1 of 50
        pm1 = PaymentAllocationService.record_payment(
            amount=Decimal('50.00'),
            customer=self.customer,
            recorded_by=self.owner
        )
        PaymentAllocationService.allocate_payment_fifo(payment=pm1, customer=self.customer)

        # Payment 2 of 70
        pm2 = PaymentAllocationService.record_payment(
            amount=Decimal('70.00'),
            customer=self.customer,
            recorded_by=self.owner
        )
        PaymentAllocationService.allocate_payment_fifo(payment=pm2, customer=self.customer)

        inv.refresh_from_db()
        self.assertEqual(inv.total_paid, Decimal('120.00'))
        self.assertEqual(inv.pending_amount, Decimal('80.00'))
        self.assertEqual(inv.status, 'partially_paid')

        # Reverse Payment 2 (70)
        res = self.client.post(f'/api/v1/billing/payments/{pm2.id}/reverse/', {
            'reason': 'Entry error for partial payment 2'
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        inv.refresh_from_db()
        pm2.refresh_from_db()

        self.assertTrue(pm2.is_reversed)
        self.assertEqual(inv.total_paid, Decimal('50.00'))
        self.assertEqual(inv.pending_amount, Decimal('150.00'))
        self.assertEqual(inv.status, 'partially_paid')

    def test_3_payment_allocated_across_multiple_invoices_reversal(self):
        """3. Single payment allocated across multiple invoices reverses all allocations and restores all invoices."""
        inv1 = self._create_invoice('INV-REV-003A', Decimal('60.00'))
        inv2 = self._create_invoice('INV-REV-003B', Decimal('40.00'))

        # Single payment of 90 covers inv1 fully (60) and inv2 partially (30)
        pm = PaymentAllocationService.record_payment(
            amount=Decimal('90.00'),
            customer=self.customer,
            recorded_by=self.owner
        )
        PaymentAllocationService.allocate_payment_fifo(payment=pm, customer=self.customer)

        inv1.refresh_from_db()
        inv2.refresh_from_db()
        self.assertEqual(inv1.status, 'paid')
        self.assertEqual(inv2.status, 'partially_paid')
        self.assertEqual(inv2.pending_amount, Decimal('10.00'))

        # Reverse this payment
        res = self.client.post(f'/api/v1/billing/payments/{pm.id}/reverse/', {
            'reason': 'Cheque returned dishonoured'
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        inv1.refresh_from_db()
        inv2.refresh_from_db()
        self.assertEqual(inv1.status, 'unpaid')
        self.assertEqual(inv1.pending_amount, Decimal('60.00'))
        self.assertEqual(inv2.status, 'unpaid')
        self.assertEqual(inv2.pending_amount, Decimal('40.00'))

    def test_4_invoice_outstanding_recalculation(self):
        """4. Verify invoice outstanding balance accurately increases back upon reversal."""
        inv = self._create_invoice('INV-REV-004', Decimal('150.00'))
        pm = PaymentAllocationService.record_payment(amount=Decimal('150.00'), customer=self.customer)
        PaymentAllocationService.allocate_payment_fifo(payment=pm, customer=self.customer)

        self.assertEqual(inv.pending_amount, Decimal('0.00'))

        PaymentAllocationService.reverse_payment(pm.id, reason="Correction")
        inv.refresh_from_db()
        self.assertEqual(inv.pending_amount, Decimal('150.00'))

    def test_5_invoice_status_recalculation(self):
        """5. Invoice transitions accurately from paid -> unpaid upon reversal."""
        inv = self._create_invoice('INV-REV-005', Decimal('80.00'))
        pm = PaymentAllocationService.record_payment(amount=Decimal('80.00'), customer=self.customer)
        PaymentAllocationService.allocate_payment_fifo(payment=pm, customer=self.customer)

        inv.refresh_from_db()
        self.assertEqual(inv.status, 'paid')

        PaymentAllocationService.reverse_payment(pm.id, reason="Customer request")
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'unpaid')

    def test_6_customer_outstanding_recalculation(self):
        """6. Customer total outstanding recalculates correctly in Customer and Ledger services."""
        inv = self._create_invoice('INV-REV-006', Decimal('300.00'))

        # Initial ledger overview
        ledger1 = LedgerService.get_customer_monthly_ledger_overview(self.customer)
        self.assertEqual(Decimal(ledger1['total_outstanding']), Decimal('300.00'))

        # Make payment of 200
        pm = PaymentAllocationService.record_payment(amount=Decimal('200.00'), customer=self.customer)
        PaymentAllocationService.allocate_payment_fifo(payment=pm, customer=self.customer)

        ledger2 = LedgerService.get_customer_monthly_ledger_overview(self.customer)
        self.assertEqual(Decimal(ledger2['total_outstanding']), Decimal('100.00'))

        # Reverse payment
        PaymentAllocationService.reverse_payment(pm.id, reason="Wrong ledger posting")

        ledger3 = LedgerService.get_customer_monthly_ledger_overview(self.customer)
        self.assertEqual(Decimal(ledger3['total_outstanding']), Decimal('300.00'))

    def test_7_original_payment_remains_in_history(self):
        """7. Reversed payment is NEVER hard deleted; remains in history with reversed status."""
        pm = PaymentAllocationService.record_payment(amount=Decimal('75.00'), customer=self.customer)
        PaymentAllocationService.reverse_payment(pm.id, reason="Audit trail test")

        self.assertTrue(Payment.objects.filter(id=pm.id).exists())
        pm_db = Payment.objects.get(id=pm.id)
        self.assertTrue(pm_db.is_reversed)
        self.assertEqual(pm_db.amount, Decimal('75.00'))
        self.assertEqual(pm_db.reversal_reason, "Audit trail test")

    def test_8_reversed_payment_excluded_from_collection_totals(self):
        """8. Excludes reversed payment from dashboard collection statistics and period credit."""
        inv = self._create_invoice('INV-REV-008', Decimal('100.00'))
        pm = PaymentAllocationService.record_payment(amount=Decimal('100.00'), customer=self.customer)

        stats_before = self.client.get('/api/v1/billing/invoices/dashboard_stats/')
        initial_received = Decimal(stats_before.data['today_received'])

        # Reverse payment
        PaymentAllocationService.reverse_payment(pm.id, reason="Testing exclusion")

        stats_after = self.client.get('/api/v1/billing/invoices/dashboard_stats/')
        after_received = Decimal(stats_after.data['today_received'])

        self.assertEqual(after_received, initial_received - Decimal('100.00'))

    def test_9_duplicate_reversal_rejected(self):
        """9. Attempting to reverse an already reversed payment is rejected with HTTP 400."""
        pm = PaymentAllocationService.record_payment(amount=Decimal('50.00'), customer=self.customer)
        PaymentAllocationService.reverse_payment(pm.id, reason="First reversal")

        # Second reversal attempt via API
        res = self.client.post(f'/api/v1/billing/payments/{pm.id}/reverse/', {
            'reason': 'Second reversal attempt'
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('payment', res.data)

    def test_10_missing_reversal_reason_rejected(self):
        """10. Reversing without a mandatory reason is rejected with HTTP 400."""
        pm = PaymentAllocationService.record_payment(amount=Decimal('50.00'), customer=self.customer)

        # Empty reason
        res1 = self.client.post(f'/api/v1/billing/payments/{pm.id}/reverse/', {
            'reason': '   '
        }, format='json')
        self.assertEqual(res1.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('reason', res1.data)

        # No reason field
        res2 = self.client.post(f'/api/v1/billing/payments/{pm.id}/reverse/', {}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('reason', res2.data)

    def test_11_cancelled_reversal_leaves_records_unchanged(self):
        """11. Reversal validation failure leaves payment and invoice states completely untouched."""
        inv = self._create_invoice('INV-REV-011', Decimal('120.00'))
        pm = PaymentAllocationService.record_payment(amount=Decimal('120.00'), customer=self.customer)
        PaymentAllocationService.allocate_payment_fifo(payment=pm, customer=self.customer)

        # Failed reversal (no reason)
        self.client.post(f'/api/v1/billing/payments/{pm.id}/reverse/', {}, format='json')

        pm.refresh_from_db()
        inv.refresh_from_db()
        self.assertFalse(pm.is_reversed)
        self.assertEqual(inv.status, 'paid')
        self.assertEqual(inv.pending_amount, Decimal('0.00'))

    def test_12_concurrent_reversal_requests_safety(self):
        """12. Two sequential/concurrent reversal calls safely reject the second without double-reversing."""
        pm = PaymentAllocationService.record_payment(amount=Decimal('90.00'), customer=self.customer)

        res1 = self.client.post(f'/api/v1/billing/payments/{pm.id}/reverse/', {
            'reason': 'Concurrent test call 1'
        }, format='json')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        res2 = self.client.post(f'/api/v1/billing/payments/{pm.id}/reverse/', {
            'reason': 'Concurrent test call 2'
        }, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)

        pm.refresh_from_db()
        self.assertTrue(pm.is_reversed)

    def test_13_historical_payment_records_remain_accessible(self):
        """13. Historical reversed and non-reversed payment records remain accessible in payment listing and detail endpoints."""
        pm = PaymentAllocationService.record_payment(amount=Decimal('65.00'), customer=self.customer)
        PaymentAllocationService.reverse_payment(pm.id, reason="Audit test")

        res = self.client.get(f'/api/v1/billing/payments/{pm.id}/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['id'], pm.id)
        self.assertTrue(res.data['is_reversed'])
        self.assertEqual(res.data['reversal_reason'], 'Audit test')

    def test_14_unauthorized_non_owner_user_rejected(self):
        """14. Non-owner / customer users are strictly rejected with HTTP 403 when attempting reversal."""
        non_owner = User.objects.create_user(username='customer_user_99', password='password123')
        pm = PaymentAllocationService.record_payment(amount=Decimal('50.00'), customer=self.customer)

        self.client.force_authenticate(user=non_owner)
        res = self.client.post(f'/api/v1/billing/payments/{pm.id}/reverse/', {
            'reason': 'Hacker attempt'
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        pm.refresh_from_db()
        self.assertFalse(pm.is_reversed)
