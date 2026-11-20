from django.test import TestCase
from django.contrib.auth.models import User
from decimal import Decimal
from django.utils import timezone
from products.models import Product, Category
from orders.models import Order, OrderItem
from billing.models import (
    Customer,
    Company,
    Invoice,
    InvoiceItem,
    InvoiceOrder,
    DraftInvoice,
    DraftInvoiceItem,
    DraftInvoiceOrder,
    Payment,
    PaymentAllocation,
)
from billing.draft_service import DraftBillingService
from rest_framework.exceptions import ValidationError


class Phase1DraftBillingArchitectureTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='owner_phase1', password='pw', is_staff=True)
        self.customer_user1 = User.objects.create_user(username='cust1', password='pw')
        self.customer_user2 = User.objects.create_user(username='cust2', password='pw')

        self.customer1 = Customer.objects.create(name='Acme Infotech', phone='9876543210', address='Tower A')
        self.customer2 = Customer.objects.create(name='Globex Corp', phone='9876543211', address='Tower B')

        self.category = Category.objects.create(name='Snacks & Tea')
        self.chai = Product.objects.create(name='Ginger Chai', price=Decimal('20.00'), category=self.category)
        self.bun = Product.objects.create(name='Bun Maska', price=Decimal('40.00'), category=self.category)

        # Create verified orders for Customer 1
        self.order1 = Order.objects.create(
            customer=self.customer_user1,
            customer_profile=self.customer1,
            status='delivered',
            total_amount=Decimal('100.00'),
            delivery_address='Tower A Floor 4'
        )
        OrderItem.objects.create(order=self.order1, product=self.chai, quantity=5, price=Decimal('20.00'))

        self.order2 = Order.objects.create(
            customer=self.customer_user1,
            customer_profile=self.customer1,
            status='delivered',
            total_amount=Decimal('80.00'),
            delivery_address='Tower A Floor 4'
        )
        OrderItem.objects.create(order=self.order2, product=self.bun, quantity=2, price=Decimal('40.00'))

        # Create order for Customer 2
        self.order3 = Order.objects.create(
            customer=self.customer_user2,
            customer_profile=self.customer2,
            status='delivered',
            total_amount=Decimal('60.00'),
            delivery_address='Tower B'
        )
        OrderItem.objects.create(order=self.order3, product=self.chai, quantity=3, price=Decimal('20.00'))

    def test_draft_invoice_creation_quick_billing(self):
        """1. Quick billing draft can be created with independent snapshot items"""
        draft = DraftBillingService.create_draft(
            customer=self.customer1,
            billing_source='quick_billing',
            items_data=[
                {'product_id': self.chai.id, 'product_name': self.chai.name, 'quantity': 10, 'unit_price': Decimal('18.00')},
                {'product_id': None, 'product_name': 'Custom Samosa Platter', 'quantity': 2, 'unit_price': Decimal('50.00')},
            ],
            discount_amount=Decimal('10.00'),
            due_date=timezone.now().date(),
            notes='Quick billing draft',
            created_by=self.owner
        )

        self.assertIsNotNone(draft.id)
        self.assertTrue(draft.draft_number.startswith('DRAFT-'))
        self.assertEqual(draft.status, 'draft')
        self.assertEqual(draft.billing_source, 'quick_billing')
        self.assertEqual(draft.subtotal, Decimal('280.00'))  # 180 + 100
        self.assertEqual(draft.discount_amount, Decimal('10.00'))
        self.assertEqual(draft.grand_total, Decimal('270.00'))
        self.assertEqual(draft.items.count(), 2)

        # Ensure no official invoice or sales metric was affected
        self.assertEqual(Invoice.objects.count(), 0)

    def test_draft_item_persistence_and_isolation(self):
        """2. Draft item edits do not mutate product catalog price"""
        draft = DraftBillingService.create_draft(
            customer=self.customer1,
            billing_source='quick_billing',
            items_data=[
                {'product_id': self.chai.id, 'product_name': 'Promo Chai', 'quantity': 5, 'unit_price': Decimal('12.00')}
            ]
        )
        item = draft.items.first()
        self.assertEqual(item.unit_price, Decimal('12.00'))

        # Product catalog price must remain unchanged
        self.chai.refresh_from_db()
        self.assertEqual(self.chai.price, Decimal('20.00'))

    def test_multiple_orders_attached_to_one_draft(self):
        """3. Multiple verified orders of the same customer can be consolidated into one draft"""
        draft = DraftBillingService.create_draft(
            customer=self.customer1,
            billing_source='consolidated_orders',
            order_ids=[self.order1.id, self.order2.id],
            discount_amount=Decimal('0.00')
        )

        self.assertEqual(draft.draft_orders.count(), 2)
        reserved_order_ids = set(draft.draft_orders.values_list('order_id', flat=True))
        self.assertEqual(reserved_order_ids, {self.order1.id, self.order2.id})
        self.assertEqual(draft.subtotal, Decimal('180.00'))  # 100 + 80

    def test_different_customer_order_consolidation_rejected(self):
        """4. Consolidating orders from different customers must be rejected"""
        with self.assertRaises(ValidationError) as ctx:
            DraftBillingService.create_draft(
                billing_source='consolidated_orders',
                order_ids=[self.order1.id, self.order3.id]  # Order 1 (Customer 1) vs Order 3 (Customer 2)
            )
        self.assertIn('order_ids', ctx.exception.detail)

    def test_duplicate_active_draft_reservation_rejected(self):
        """5. An order cannot be reserved by multiple active drafts"""
        # Create first active draft reserving order 1
        DraftBillingService.create_draft(
            customer=self.customer1,
            billing_source='single_order',
            order_ids=[self.order1.id]
        )

        # Attempt to create second active draft reserving order 1
        with self.assertRaises(ValidationError) as ctx:
            DraftBillingService.create_draft(
                customer=self.customer1,
                billing_source='consolidated_orders',
                order_ids=[self.order1.id, self.order2.id]
            )
        self.assertIn('order_ids', ctx.exception.detail)

    def test_draft_cancellation_releases_reservation(self):
        """6. Cancelling a draft releases its reserved orders for new drafts"""
        draft1 = DraftBillingService.create_draft(
            customer=self.customer1,
            billing_source='single_order',
            order_ids=[self.order1.id]
        )
        self.assertEqual(draft1.status, 'draft')

        # Cancel draft 1
        DraftBillingService.cancel_draft(draft1)
        draft1.refresh_from_db()
        self.assertEqual(draft1.status, 'cancelled')

        # Now creating draft 2 with order 1 must succeed
        draft2 = DraftBillingService.create_draft(
            customer=self.customer1,
            billing_source='consolidated_orders',
            order_ids=[self.order1.id, self.order2.id]
        )
        self.assertEqual(draft2.status, 'draft')
        self.assertEqual(draft2.draft_orders.count(), 2)

    def test_one_official_invoice_linked_to_multiple_orders_via_invoice_order(self):
        """7. Official invoice can consolidate multiple orders via InvoiceOrder linking model"""
        invoice = Invoice.objects.create(
            invoice_number='INV-20261120-0099',
            customer=self.customer1,
            customer_name=self.customer1.name,
            subtotal=Decimal('180.00'),
            grand_total=Decimal('180.00'),
            status='unpaid',
            due_date=timezone.now().date(),
            finalized_at=timezone.now()
        )

        links = DraftBillingService.attach_orders_to_official_invoice(invoice, [self.order1, self.order2])
        self.assertEqual(len(links), 2)
        self.assertEqual(InvoiceOrder.objects.filter(invoice=invoice).count(), 2)

        # Order has_invoice property check
        self.order1.refresh_from_db()
        self.order2.refresh_from_db()
        self.assertTrue(self.order1.has_invoice)
        self.assertTrue(self.order2.has_invoice)
        self.assertEqual(self.order1.official_invoice, invoice)

    def test_order_cannot_be_attached_to_two_official_invoices(self):
        """8. An order cannot be attached to two official invoices (duplicate protection)"""
        invoice1 = Invoice.objects.create(
            invoice_number='INV-20261120-0101',
            customer=self.customer1,
            subtotal=Decimal('100.00'),
            grand_total=Decimal('100.00')
        )
        DraftBillingService.attach_orders_to_official_invoice(invoice1, [self.order1])

        invoice2 = Invoice.objects.create(
            invoice_number='INV-20261120-0102',
            customer=self.customer1,
            subtotal=Decimal('100.00'),
            grand_total=Decimal('100.00')
        )
        with self.assertRaises(ValidationError) as ctx:
            DraftBillingService.attach_orders_to_official_invoice(invoice2, [self.order1])
        self.assertIn('order', ctx.exception.detail)

    def test_legacy_invoice_order_backward_compatibility(self):
        """9. Legacy Invoice.order OneToOne relationship continues to function seamlessly"""
        legacy_invoice = Invoice.objects.create(
            invoice_number='INV-20261120-0103',
            order=self.order3,
            customer=self.customer2,
            subtotal=Decimal('60.00'),
            grand_total=Decimal('60.00')
        )
        self.order3.refresh_from_db()
        self.assertTrue(self.order3.has_invoice)
        self.assertEqual(self.order3.official_invoice, legacy_invoice)

        # Attempting to re-invoice via DraftBillingService must reject
        another_invoice = Invoice.objects.create(
            invoice_number='INV-20261120-0104',
            customer=self.customer2,
            grand_total=Decimal('60.00')
        )
        with self.assertRaises(ValidationError):
            DraftBillingService.attach_orders_to_official_invoice(another_invoice, [self.order3])

    def test_quick_billing_standalone_invoice_has_zero_invoice_orders(self):
        """10. Quick Billing standalone invoices have order=None and zero InvoiceOrder records"""
        qb_invoice = Invoice.objects.create(
            invoice_number='INV-20261120-0105',
            customer=self.customer1,
            subtotal=Decimal('50.00'),
            grand_total=Decimal('50.00')
        )
        self.assertIsNone(qb_invoice.order)
        self.assertEqual(qb_invoice.invoice_orders.count(), 0)
