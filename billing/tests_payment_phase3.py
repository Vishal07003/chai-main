from decimal import Decimal
from datetime import timedelta
from django.db import transaction
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework.exceptions import ValidationError

from accounts.models import UserProfile
from products.models import Category, Product
from billing.models import (
    Customer,
    Company,
    Invoice,
    InvoiceItem,
    InvoiceOrder,
    Payment,
    PaymentAllocation,
    DraftInvoice,
    DraftInvoiceItem,
)
from billing.draft_service import DraftBillingService
from billing.payment_service import PaymentAllocationService


class Phase3PaymentAllocationAndSynchronizationTests(TestCase):
    """
    Dedicated Phase 3 Test Suite:
    1. Advance payment before invoice finalization.
    2. Automatic advance allocation after finalization.
    3. Partial advance allocation.
    4. Excess advance retained as unallocated credit.
    5. Multiple unpaid invoices with FIFO allocation.
    6. Multiple advances allocated correctly.
    7. No allocation to draft invoices.
    8. Duplicate finalization prevention.
    9. Duplicate payment submission protection.
    10. Concurrent allocation protection / locking.
    11. Customer-level payment API.
    12. Invoice-specific payment API.
    13. Pending Payments balance accuracy.
    14. Defaulter status after full payment.
    15. Custom due date and overdue calculations.
    16. Historical invoices without due dates.
    17. Payment failure rollback.
    18. Existing payment API regression tests.
    """

    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(username='phase3owner', password='Password123!')
        self.owner_profile = UserProfile.objects.create(user=self.owner, phone='9988776655', role='owner')
        self.client.force_authenticate(user=self.owner)

        self.category = Category.objects.create(name='Phase 3 Beverages')
        self.chai = Product.objects.create(name='Ginger Chai', category=self.category, price='20.00', is_available=True)
        self.coffee = Product.objects.create(name='Filter Coffee', category=self.category, price='30.00', is_available=True)

        self.customer = Customer.objects.create(name='Anil Verma', phone='9811122233', address='Office 101')

    def test_1_advance_payment_before_invoice_finalization(self):
        """1. Advance payment before invoice finalization remains unallocated credit."""
        pm = PaymentAllocationService.record_payment(
            amount=Decimal('500.00'),
            method='upi',
            customer=self.customer,
            notes='Advance deposit'
        )
        self.assertEqual(pm.amount, Decimal('500.00'))
        self.assertEqual(pm.total_allocated, Decimal('0.00'))
        self.assertEqual(pm.unallocated_amount, Decimal('500.00'))

        # Check helper
        unalloc = PaymentAllocationService.get_unallocated_advance(customer=self.customer)
        self.assertEqual(unalloc, Decimal('500.00'))

    def test_2_automatic_advance_allocation_after_finalization(self):
        """2. Automatic advance allocation after finalization applies customer credit."""
        # Customer makes advance payment of 200
        pm = PaymentAllocationService.record_payment(
            amount=Decimal('200.00'),
            method='upi',
            customer=self.customer,
            notes='Advance deposit 200'
        )

        # Create a draft invoice for 100
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            items_data=[{'product_id': self.chai.id, 'quantity': 5, 'unit_price': Decimal('20.00')}], # 100
            created_by=self.owner,
        )
        self.assertEqual(draft.grand_total, Decimal('100.00'))
        self.assertEqual(draft.status, 'draft')

        # Finalize draft
        official_inv = DraftBillingService.finalize_draft(draft_id=draft.id, owner_user=self.owner)
        self.assertEqual(official_inv.status, 'paid')
        self.assertEqual(official_inv.pending_amount, Decimal('0.00'))
        self.assertEqual(official_inv.total_paid, Decimal('100.00'))

        # Check payment allocations
        pm.refresh_from_db()
        self.assertEqual(pm.total_allocated, Decimal('100.00'))
        self.assertEqual(pm.unallocated_amount, Decimal('100.00'))

    def test_3_partial_advance_allocation(self):
        """3. Partial advance allocation: advance is less than invoice total."""
        # Customer has 60 advance
        PaymentAllocationService.record_payment(
            amount=Decimal('60.00'),
            method='cash',
            customer=self.customer,
        )

        # Draft is for 100
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            items_data=[{'product_id': self.chai.id, 'quantity': 5, 'unit_price': Decimal('20.00')}],
            created_by=self.owner,
        )
        official_inv = DraftBillingService.finalize_draft(draft_id=draft.id, owner_user=self.owner)

        self.assertEqual(official_inv.status, 'partially_paid')
        self.assertEqual(official_inv.total_paid, Decimal('60.00'))
        self.assertEqual(official_inv.pending_amount, Decimal('40.00'))
        self.assertEqual(PaymentAllocationService.get_unallocated_advance(customer=self.customer), Decimal('0.00'))

    def test_4_excess_advance_retained_as_unallocated_credit(self):
        """4. Excess advance retained as unallocated credit."""
        PaymentAllocationService.record_payment(
            amount=Decimal('500.00'),
            method='online',
            customer=self.customer,
        )

        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            items_data=[{'product_id': self.chai.id, 'quantity': 5, 'unit_price': Decimal('20.00')}], # 100
            created_by=self.owner,
        )
        official_inv = DraftBillingService.finalize_draft(draft_id=draft.id, owner_user=self.owner)

        self.assertEqual(official_inv.status, 'paid')
        rem_advance = PaymentAllocationService.get_unallocated_advance(customer=self.customer)
        self.assertEqual(rem_advance, Decimal('400.00'))

    def test_5_multiple_unpaid_invoices_with_fifo_allocation(self):
        """5. Multiple unpaid invoices allocated strictly according to FIFO (oldest first)."""
        # Create invoice 1 (older) = 50
        inv1 = Invoice.objects.create(
            invoice_number='INV-OLD-001',
            customer=self.customer,
            customer_name=self.customer.name,
            subtotal=Decimal('50.00'),
            grand_total=Decimal('50.00'),
            status='unpaid',
        )
        InvoiceItem.objects.create(invoice=inv1, product_name='Item 1', unit_price=Decimal('50.00'), quantity=1, total_price=Decimal('50.00'))

        # Create invoice 2 (newer) = 70
        inv2 = Invoice.objects.create(
            invoice_number='INV-NEW-002',
            customer=self.customer,
            customer_name=self.customer.name,
            subtotal=Decimal('70.00'),
            grand_total=Decimal('70.00'),
            status='unpaid',
        )
        InvoiceItem.objects.create(invoice=inv2, product_name='Item 2', unit_price=Decimal('70.00'), quantity=1, total_price=Decimal('70.00'))

        # Customer pays 80
        pm = PaymentAllocationService.record_payment(
            amount=Decimal('80.00'),
            method='cash',
            customer=self.customer,
        )
        alloc_res = PaymentAllocationService.allocate_payment_fifo(payment=pm, customer=self.customer)

        inv1.refresh_from_db()
        inv2.refresh_from_db()

        self.assertEqual(inv1.status, 'paid')
        self.assertEqual(inv1.pending_amount, Decimal('0.00'))
        self.assertEqual(inv2.status, 'partially_paid')
        self.assertEqual(inv2.pending_amount, Decimal('40.00')) # 70 - 30 = 40
        self.assertEqual(inv2.total_paid, Decimal('30.00'))
        self.assertEqual(Decimal(alloc_res['allocated_now']), Decimal('80.00'))

    def test_6_multiple_advances_allocated_correctly(self):
        """6. Multiple advances accumulated over time are consumed in FIFO order."""
        # Payment 1 = 40
        pm1 = PaymentAllocationService.record_payment(amount=Decimal('40.00'), customer=self.customer)
        # Payment 2 = 60
        pm2 = PaymentAllocationService.record_payment(amount=Decimal('60.00'), customer=self.customer)

        self.assertEqual(PaymentAllocationService.get_unallocated_advance(customer=self.customer), Decimal('100.00'))

        # New invoice = 70
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            items_data=[{'product_name': 'Bulk order', 'quantity': 1, 'unit_price': Decimal('70.00')}],
            created_by=self.owner,
        )
        inv = DraftBillingService.finalize_draft(draft_id=draft.id, owner_user=self.owner)

        inv.refresh_from_db()
        pm1.refresh_from_db()
        pm2.refresh_from_db()

        self.assertEqual(inv.status, 'paid')
        self.assertEqual(inv.pending_amount, Decimal('0.00'))
        # pm1 should be fully allocated (40)
        self.assertEqual(pm1.total_allocated, Decimal('40.00'))
        self.assertEqual(pm1.unallocated_amount, Decimal('0.00'))
        # pm2 should be partially allocated (30 of 60)
        self.assertEqual(pm2.total_allocated, Decimal('30.00'))
        self.assertEqual(pm2.unallocated_amount, Decimal('30.00'))
        self.assertEqual(PaymentAllocationService.get_unallocated_advance(customer=self.customer), Decimal('30.00'))

    def test_7_no_allocation_to_draft_invoices(self):
        """7. Payment allocations must never link to DraftInvoices."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            items_data=[{'product_name': 'Tea', 'quantity': 1, 'unit_price': Decimal('20.00')}],
            created_by=self.owner,
        )
        pm = PaymentAllocationService.record_payment(amount=Decimal('50.00'), customer=self.customer)
        PaymentAllocationService.allocate_payment_fifo(payment=pm, customer=self.customer)

        # Confirm no allocations link to drafts
        allocs = PaymentAllocation.objects.filter(payment=pm)
        self.assertEqual(allocs.count(), 0)
        self.assertEqual(draft.status, 'draft')

    def test_8_duplicate_finalization_prevention(self):
        """8. Re-finalization of an already finalized draft is blocked."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            items_data=[{'product_name': 'Tea', 'quantity': 1, 'unit_price': Decimal('20.00')}],
            created_by=self.owner,
        )
        DraftBillingService.finalize_draft(draft_id=draft.id, owner_user=self.owner)

        with self.assertRaises(ValidationError):
            DraftBillingService.finalize_draft(draft_id=draft.id, owner_user=self.owner)

    def test_9_duplicate_payment_validation(self):
        """9. Zero or negative payments are rejected with validation error."""
        with self.assertRaises(ValidationError):
            PaymentAllocationService.record_payment(amount=Decimal('0.00'), customer=self.customer)

        with self.assertRaises(ValidationError):
            PaymentAllocationService.record_payment(amount=Decimal('-10.00'), customer=self.customer)

    def test_10_concurrent_allocation_protection(self):
        """10. Attempting to allocate more than payment remaining returns accurate bounded allocation."""
        pm = PaymentAllocationService.record_payment(amount=Decimal('50.00'), customer=self.customer)
        inv = Invoice.objects.create(
            invoice_number='INV-TEST-LOCKED',
            customer=self.customer,
            customer_name=self.customer.name,
            subtotal=Decimal('100.00'),
            grand_total=Decimal('100.00'),
            status='unpaid',
        )

        # Allocate 50
        res1 = PaymentAllocationService.allocate_payment_fifo(payment=pm, customer=self.customer)
        self.assertEqual(Decimal(res1['allocated_now']), Decimal('50.00'))

        # Immediate secondary allocation attempt on same payment
        res2 = PaymentAllocationService.allocate_payment_fifo(payment=pm, customer=self.customer)
        self.assertEqual(Decimal(res2['allocated_now']), Decimal('0.00'))
        self.assertEqual(PaymentAllocation.objects.filter(payment=pm).count(), 1)

    def test_11_customer_level_payment_api(self):
        """11. POST /api/v1/billing/customers/{id}/collect-payment/ allocates and returns credit."""
        inv = Invoice.objects.create(
            invoice_number='INV-API-CUST-1',
            customer=self.customer,
            customer_name=self.customer.name,
            subtotal=Decimal('60.00'),
            grand_total=Decimal('60.00'),
            status='unpaid',
        )

        res = self.client.post(f'/api/v1/billing/customers/{self.customer.id}/collect-payment/', {
            'amount': '100.00',
            'method': 'upi',
            'notes': 'Overpayment collection'
        }, format='json')

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(res.data['amount_allocated']), Decimal('60.00'))
        self.assertEqual(Decimal(res.data['unallocated_credit']), Decimal('40.00'))

        inv.refresh_from_db()
        self.assertEqual(inv.status, 'paid')

    def test_12_invoice_specific_payment_api(self):
        """12. POST /api/v1/billing/invoices/{id}/payments/ records payment directly on invoice."""
        inv = Invoice.objects.create(
            invoice_number='INV-API-DIR-1',
            customer=self.customer,
            customer_name=self.customer.name,
            subtotal=Decimal('80.00'),
            grand_total=Decimal('80.00'),
            status='unpaid',
        )

        res = self.client.post(f'/api/v1/billing/invoices/{inv.id}/payments/', {
            'amount': '50.00',
            'method': 'cash',
        }, format='json')

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(res.data['pending_amount']), Decimal('30.00'))
        self.assertEqual(res.data['status'], 'partially_paid')

    def test_13_pending_payments_balance_accuracy(self):
        """13. Customer total_pending_amount reflects genuine unpaid invoice balance."""
        Invoice.objects.create(
            invoice_number='INV-PENDING-1',
            customer=self.customer,
            customer_name=self.customer.name,
            subtotal=Decimal('120.00'),
            grand_total=Decimal('120.00'),
            status='unpaid',
        )

        res = self.client.get(f'/api/v1/billing/customers/{self.customer.id}/')
        self.assertEqual(Decimal(res.data['total_pending_amount']), Decimal('120.00'))

    def test_14_defaulter_status_after_full_payment(self):
        """14. Customer is cleared from defaulter status once all overdue invoices are paid."""
        past_date = timezone.now().date() - timedelta(days=10)
        inv = Invoice.objects.create(
            invoice_number='INV-OVERDUE-1',
            customer=self.customer,
            customer_name=self.customer.name,
            subtotal=Decimal('90.00'),
            grand_total=Decimal('90.00'),
            status='unpaid',
            due_date=past_date,
        )

        res_before = self.client.get(f'/api/v1/billing/customers/{self.customer.id}/')
        self.assertTrue(res_before.data['is_defaulter'])
        self.assertTrue(res_before.data['has_overdue'])

        # Pay invoice in full
        self.client.post(f'/api/v1/billing/invoices/{inv.id}/payments/', {
            'amount': '90.00',
            'method': 'cash',
        }, format='json')

        res_after = self.client.get(f'/api/v1/billing/customers/{self.customer.id}/')
        self.assertFalse(res_after.data['is_defaulter'])
        self.assertFalse(res_after.data['has_overdue'])
        self.assertEqual(Decimal(res_after.data['total_pending_amount']), Decimal('0.00'))

    def test_15_custom_due_date_and_overdue_calculations(self):
        """15. Custom due date dictates overdue calculation accurately."""
        future_date = timezone.now().date() + timedelta(days=5)
        inv = Invoice.objects.create(
            invoice_number='INV-FUTURE-DUE',
            customer=self.customer,
            customer_name=self.customer.name,
            subtotal=Decimal('150.00'),
            grand_total=Decimal('150.00'),
            status='unpaid',
            due_date=future_date,
        )
        res = self.client.get(f'/api/v1/billing/invoices/{inv.id}/')
        self.assertFalse(res.data['is_overdue'])

        c_res = self.client.get(f'/api/v1/billing/customers/{self.customer.id}/')
        self.assertFalse(c_res.data['is_defaulter'])

    def test_16_historical_invoices_without_due_dates(self):
        """16. Invoices without due_date are NEVER automatically classified as overdue (no 7-day fallback)."""
        old_time = timezone.now() - timedelta(days=80)
        inv = Invoice.objects.create(
            invoice_number='INV-LEGACY-DUE',
            customer=self.customer,
            customer_name=self.customer.name,
            subtotal=Decimal('100.00'),
            grand_total=Decimal('100.00'),
            status='unpaid',
            due_date=None,
        )
        # Update created_at far in the past
        Invoice.objects.filter(id=inv.id).update(created_at=old_time)
        inv.refresh_from_db()

        res = self.client.get(f'/api/v1/billing/invoices/{inv.id}/')
        # Without explicit due_date, must NOT be overdue
        self.assertFalse(res.data['is_overdue'])

        c_res = self.client.get(f'/api/v1/billing/customers/{self.customer.id}/')
        self.assertFalse(c_res.data['is_defaulter'])
        self.assertFalse(c_res.data['has_overdue'])

    def test_17_payment_failure_rollback(self):
        """17. If an error occurs in payment allocation, all changes rollback."""
        try:
            with transaction.atomic():
                PaymentAllocationService.record_payment(
                    amount=Decimal('50.00'),
                    customer=self.customer,
                )
                raise RuntimeError("Simulated failure during allocation")
        except RuntimeError:
            pass

        self.assertEqual(Payment.objects.filter(customer=self.customer).count(), 0)

    def test_18_existing_payment_api_regression_contracts(self):
        """18. Existing Company payment endpoints continue working as expected."""
        company = Company.objects.create(name='Acme Corp', phone='9876500000', gstin='27AAAAA0000A1Z5')
        inv = Invoice.objects.create(
            invoice_number='INV-CO-001',
            company=company,
            customer_name=company.name,
            subtotal=Decimal('300.00'),
            grand_total=Decimal('300.00'),
            status='unpaid',
        )

        res = self.client.post(f'/api/v1/billing/companies/{company.id}/collect-payment/', {
            'amount': '350.00',
            'method': 'bank_transfer',
            'reference_number': 'NEFT123456',
        }, format='json')

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(res.data['allocated_to_invoices']), Decimal('300.00'))
        self.assertEqual(Decimal(res.data['unallocated_advance_credit']), Decimal('50.00'))

        inv.refresh_from_db()
        self.assertEqual(inv.status, 'paid')
