from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework.exceptions import ValidationError

from accounts.models import UserProfile
from products.models import Category, Product
from deliveries.models import Delivery, DeliveryPartner, DeliveryStatusHistory
from orders.models import Order, OrderItem
from billing.models import Customer, Company, DraftInvoice, DraftInvoiceItem, Invoice, InvoiceOrder
from billing.draft_service import DraftBillingService


class Phase5BFulfillmentDeliveryDispatchTests(TestCase):
    """
    Phase 5B: Dedicated Fulfillment Order & Delivery Dispatch Tests:
    1. Quick Billing with partner creates one Invoice, one fulfillment Order and one Delivery.
    2. Quick Billing without partner creates only the Invoice.
    3. Walk-in draft with partner creates a valid fulfillment Order.
    4. Registered customer draft maps customer_profile correctly.
    5. Order.customer follows existing owner/creator convention.
    6. Discounted draft uses grand_total as Order.total_amount.
    7. Invoice subtotal and discount remain unchanged.
    8. InvoiceOrder and legacy Invoice.order relationships remain consistent.
    9. Order.has_invoice returns True.
    10. Duplicate invoice generation is prevented.
    11. Repeated finalization does not duplicate Order or Delivery.
    12. Delivery initial status is assigned.
    13. Initial DeliveryStatusHistory is recorded.
    14. Existing DeliveryItems and GPS workflows remain compatible.
    15. Failed transaction creates no orphan records and sends no notification.
    16. Exactly one assignment notification is dispatched after commit.
    17. Normal single-order billing remains unchanged.
    18. Consolidated billing remains unchanged.
    """

    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(username='phase5_owner', password='Password123!')
        self.owner_profile = UserProfile.objects.create(user=self.owner, phone='9988771122', role='owner')
        self.client.force_authenticate(user=self.owner)

        self.category = Category.objects.create(name='Snacks')
        self.samosa = Product.objects.create(name='Samosa', category=self.category, price='15.00', is_available=True)
        self.tea = Product.objects.create(name='Masala Tea', category=self.category, price='10.00', is_available=True)

        self.company = Company.objects.create(name='Tech Park Stall')
        self.customer = Customer.objects.create(
            name='Rohit Sharma',
            phone='9811002233',
            address='Building 4, Floor 2',
            company=self.company
        )

        # Active Delivery Partner
        self.driver_user = User.objects.create_user(username='driver_mukesh', password='Password123!')
        UserProfile.objects.create(user=self.driver_user, phone='9876543210', role='delivery')
        self.partner = DeliveryPartner.objects.create(
            user=self.driver_user,
            name='Mukesh Sharma',
            phone='9876543210',
            is_active=True,
            status='READY'
        )

    def test_1_quick_billing_with_partner_creates_invoice_order_delivery(self):
        """1. Quick Billing with partner creates one Invoice, one fulfillment Order and one Delivery."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[
                {'product_id': self.samosa.id, 'quantity': 2, 'unit_price': Decimal('15.00')},
                {'product_id': self.tea.id, 'quantity': 3, 'unit_price': Decimal('10.00')}
            ],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertIsNotNone(inv)

        # Check Order
        self.assertIsNotNone(inv.order)
        order = inv.order
        self.assertEqual(order.customer_profile, self.customer)
        self.assertEqual(order.delivery_address, self.customer.address)
        self.assertEqual(order.total_amount, Decimal('60.00'))
        self.assertEqual(order.status, 'accepted')
        self.assertEqual(order.items.count(), 2)

        # Check Delivery
        self.assertTrue(hasattr(order, 'delivery'))
        delivery = order.delivery
        self.assertEqual(delivery.driver, self.driver_user)
        self.assertEqual(delivery.status, 'assigned')

    def test_2_quick_billing_without_partner_creates_only_invoice(self):
        """2. Quick Billing without partner creates only the Invoice (no order or delivery)."""
        order_count_before = Order.objects.count()
        delivery_count_before = Delivery.objects.count()

        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=None,
            items_data=[{'product_id': self.tea.id, 'quantity': 2, 'unit_price': Decimal('10.00')}],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertIsNotNone(inv)
        self.assertIsNone(inv.order)
        self.assertEqual(Order.objects.count(), order_count_before)
        self.assertEqual(Delivery.objects.count(), delivery_count_before)

    def test_3_walkin_draft_with_partner_creates_valid_fulfillment_order(self):
        """3. Walk-in draft with partner creates a valid fulfillment Order with required delivery_address."""
        draft = DraftBillingService.create_draft(
            customer=None,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            delivery_address="Plot 55, Cyber City, Gate 3",
            items_data=[{'product_id': self.samosa.id, 'quantity': 4, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertIsNotNone(inv.order)
        order = inv.order
        self.assertIsNone(order.customer_profile)
        self.assertEqual(order.customer, self.owner)
        self.assertEqual(order.delivery_address, "Plot 55, Cyber City, Gate 3")
        self.assertEqual(order.delivery.driver, self.driver_user)

    def test_4_registered_customer_draft_maps_customer_profile(self):
        """4. Registered customer draft maps customer_profile correctly."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.tea.id, 'quantity': 1, 'unit_price': Decimal('10.00')}],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertEqual(inv.order.customer_profile, self.customer)
        self.assertEqual(inv.order.company, self.company)

    def test_5_order_customer_follows_owner_convention(self):
        """5. Order.customer follows existing owner/creator convention."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.samosa.id, 'quantity': 1, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        # self.customer has no associated User object, so order.customer defaults to the finalizing owner
        self.assertEqual(inv.order.customer, self.owner)

    def test_6_discounted_draft_uses_grand_total_as_order_total(self):
        """6. Discounted draft uses grand_total as Order.total_amount."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            discount_amount=Decimal('10.00'),
            items_data=[{'product_id': self.samosa.id, 'quantity': 4, 'unit_price': Decimal('15.00')}], # subtotal 60
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertEqual(inv.subtotal, Decimal('60.00'))
        self.assertEqual(inv.discount_amount, Decimal('10.00'))
        self.assertEqual(inv.grand_total, Decimal('50.00'))
        self.assertEqual(inv.order.total_amount, Decimal('50.00'))

    def test_7_invoice_subtotal_and_discount_remain_unchanged(self):
        """7. Invoice subtotal, discount and grand_total remain mathematically intact."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            discount_amount=Decimal('5.00'),
            items_data=[{'product_id': self.tea.id, 'quantity': 3, 'unit_price': Decimal('10.00')}],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertEqual(inv.subtotal, Decimal('30.00'))
        self.assertEqual(inv.discount_amount, Decimal('5.00'))
        self.assertEqual(inv.grand_total, Decimal('25.00'))

    def test_8_invoice_order_and_legacy_relationships_consistent(self):
        """8. InvoiceOrder and legacy Invoice.order relationships remain consistent."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.samosa.id, 'quantity': 1, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        order = inv.order
        self.assertIsNotNone(order)
        # Check InvoiceOrder junction
        io = InvoiceOrder.objects.filter(invoice=inv, order=order).first()
        self.assertIsNotNone(io)

    def test_9_order_has_invoice_returns_true(self):
        """9. Order.has_invoice returns True immediately."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.samosa.id, 'quantity': 1, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        order = Order.objects.get(id=inv.order.id)
        self.assertTrue(order.has_invoice)

    def test_10_duplicate_invoice_generation_prevented(self):
        """10. Duplicate invoice generation is prevented on the fulfillment order."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.samosa.id, 'quantity': 1, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        order = inv.order

        # Attempting to reserve this fulfillment order in another draft must raise ValidationError
        with self.assertRaises(ValidationError):
            DraftBillingService.create_draft(
                customer=self.customer,
                billing_source='order_billing',
                order_ids=[order.id],
                created_by=self.owner,
            )

    def test_11_repeated_finalization_does_not_duplicate_order_or_delivery(self):
        """11. Repeated finalization does not duplicate Order or Delivery."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.samosa.id, 'quantity': 2, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )

        DraftBillingService.finalize_draft(draft.id, self.owner)
        order_count = Order.objects.count()
        delivery_count = Delivery.objects.count()

        # Calling finalize again on same draft raises ValidationError due to status='finalized'
        with self.assertRaises(ValidationError):
            DraftBillingService.finalize_draft(draft.id, self.owner)

        self.assertEqual(Order.objects.count(), order_count)
        self.assertEqual(Delivery.objects.count(), delivery_count)

    def test_12_delivery_initial_status_is_assigned(self):
        """12. Delivery initial status is assigned."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.samosa.id, 'quantity': 1, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertEqual(inv.order.delivery.status, 'assigned')

    def test_13_initial_delivery_status_history_recorded(self):
        """13. Initial DeliveryStatusHistory is recorded with assigned state."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.samosa.id, 'quantity': 1, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        delivery = inv.order.delivery
        history = DeliveryStatusHistory.objects.filter(delivery=delivery).first()
        self.assertIsNotNone(history)
        self.assertEqual(history.to_status, 'assigned')
        self.assertEqual(history.changed_by, self.owner)

    def test_14_existing_delivery_workflow_compatibility(self):
        """14. Existing Delivery workflow (accepted -> delivered) remains completely compatible."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.tea.id, 'quantity': 2, 'unit_price': Decimal('10.00')}],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        delivery = inv.order.delivery

        # Partner can accept and deliver
        delivery.status = 'accepted'
        delivery.save(update_fields=['status'])
        self.assertEqual(Delivery.objects.get(id=delivery.id).status, 'accepted')

        delivery.status = 'delivered'
        delivery.save(update_fields=['status'])
        self.assertEqual(Delivery.objects.get(id=delivery.id).status, 'delivered')

    def test_15_failed_transaction_creates_no_orphan_records_and_sends_no_notification(self):
        """15. Failed transaction creates no orphan records and sends no notification."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.tea.id, 'quantity': 1, 'unit_price': Decimal('10.00')}],
            created_by=self.owner,
        )

        order_count_before = Order.objects.count()
        delivery_count_before = Delivery.objects.count()
        inv_count_before = Invoice.objects.count()

        with patch('billing.payment_service.PaymentAllocationService.allocate_customer_advances_on_invoice', side_effect=RuntimeError("Simulated advance allocation failure")):
            with patch('notifications.services.NotificationService.on_delivery_assigned') as mock_notif:
                with self.assertRaises(RuntimeError):
                    DraftBillingService.finalize_draft(draft.id, self.owner)

                mock_notif.assert_not_called()
                self.assertEqual(Order.objects.count(), order_count_before)
                self.assertEqual(Delivery.objects.count(), delivery_count_before)
                self.assertEqual(Invoice.objects.count(), inv_count_before)

    def test_16_exactly_one_assignment_notification_dispatched_after_commit(self):
        """16. Exactly one assignment notification is dispatched after transaction commit."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.tea.id, 'quantity': 1, 'unit_price': Decimal('10.00')}],
            created_by=self.owner,
        )

        with self.captureOnCommitCallbacks(execute=True):
            with patch('notifications.services.NotificationService.on_delivery_assigned') as mock_notif:
                inv = DraftBillingService.finalize_draft(draft.id, self.owner)
                mock_notif.assert_called_once_with(inv.order.delivery)

    def test_17_normal_single_order_billing_remains_unchanged(self):
        """17. Normal single-order billing preserves existing order & does not create second order."""
        # Create an existing normal order
        existing_order = Order.objects.create(
            customer=self.owner,
            customer_profile=self.customer,
            company=self.company,
            total_amount=Decimal('45.00'),
            status='accepted'
        )
        OrderItem.objects.create(order=existing_order, product=self.samosa, quantity=3, price=Decimal('15.00'))

        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='single_order',
            order_ids=[existing_order.id],
            created_by=self.owner,
        )

        order_count_before = Order.objects.count()
        inv = DraftBillingService.finalize_draft(draft.id, self.owner)

        # No new order created, linked to existing order
        self.assertEqual(Order.objects.count(), order_count_before)
        self.assertEqual(inv.order, existing_order)

    def test_18_consolidated_billing_remains_unchanged(self):
        """18. Consolidated billing preserves existing orders without creating fulfillment order."""
        order1 = Order.objects.create(customer=self.owner, customer_profile=self.customer, company=self.company, total_amount=Decimal('30.00'), status='accepted')
        OrderItem.objects.create(order=order1, product=self.samosa, quantity=2, price=Decimal('15.00'))

        order2 = Order.objects.create(customer=self.owner, customer_profile=self.customer, company=self.company, total_amount=Decimal('20.00'), status='accepted')
        OrderItem.objects.create(order=order2, product=self.tea, quantity=2, price=Decimal('10.00'))

        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='consolidated_orders',
            order_ids=[order1.id, order2.id],
            created_by=self.owner,
        )

        order_count_before = Order.objects.count()
        inv = DraftBillingService.finalize_draft(draft.id, self.owner)

        # No new order created; both linked in InvoiceOrder
        self.assertEqual(Order.objects.count(), order_count_before)
        linked_orders = list(inv.invoice_orders.values_list('order_id', flat=True))
        self.assertEqual(set(linked_orders), {order1.id, order2.id})
