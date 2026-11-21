from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework.exceptions import ValidationError

from accounts.models import UserProfile
from products.models import Category, Product
from deliveries.models import Delivery, DeliveryPartner
from orders.models import Order
from billing.models import Customer, Company, DraftInvoice, Invoice
from billing.draft_service import DraftBillingService


class Phase5C1DeliveryAddressSupportTests(TestCase):
    """
    Phase 5C.1: Delivery Destination Address Support Verification Tests:
    1. Registered customer with saved address and partner automatically resolves address.
    2. Registered customer with edited destination and partner uses custom destination without modifying saved customer address.
    3. Walk-in with partner and valid address succeeds and persists address.
    4. Walk-in with partner and missing address is rejected (ValidationError).
    5. Walk-in without partner: normal counter billing succeeds with empty address.
    6. Registered customer without partner: address optional.
    7. Partner removed after entering address: updates cleanly.
    8. Existing drafts without delivery_address remain readable and editable.
    9. Draft edit and reload preserves address.
    10. Finalized fulfillment Order receives the correct destination.
    11. Delivery partner serializer / app receives the correct destination.
    12. Normal ORDER mode remains unchanged.
    13. Customer permanent address is never modified by draft-specific address editing.
    """

    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(username='phase5c1_owner', password='Password123!')
        self.owner_profile = UserProfile.objects.create(user=self.owner, phone='9988771122', role='owner')
        self.client.force_authenticate(user=self.owner)

        self.category = Category.objects.create(name='Snacks')
        self.samosa = Product.objects.create(name='Samosa', category=self.category, price='15.00', is_available=True)

        self.customer = Customer.objects.create(
            name='Kavita Krishnan',
            phone='9811002233',
            address='Flat 402, Green Glen Layout, Bellandur',
        )

        # Active Delivery Partner
        self.driver_user = User.objects.create_user(username='driver_sunil', password='Password123!')
        UserProfile.objects.create(user=self.driver_user, phone='9876543211', role='delivery')
        self.partner = DeliveryPartner.objects.create(
            user=self.driver_user,
            name='Sunil Verma',
            phone='9876543211',
            is_active=True,
            status='READY'
        )

    def test_1_registered_customer_with_saved_address_and_partner(self):
        """1. Registered customer with saved address and partner automatically resolves customer address."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.samosa.id, 'quantity': 2, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )
        self.assertEqual(draft.delivery_address, self.customer.address)

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertEqual(inv.order.delivery_address, self.customer.address)

    def test_2_registered_customer_with_edited_destination(self):
        """2. Registered customer with custom destination uses overridden address without altering customer model."""
        custom_dest = "Office Block B, 5th Floor, EcoSpace"
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            delivery_address=custom_dest,
            items_data=[{'product_id': self.samosa.id, 'quantity': 2, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )
        self.assertEqual(draft.delivery_address, custom_dest)

        # Verify customer's permanent address did not change
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.address, 'Flat 402, Green Glen Layout, Bellandur')

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertEqual(inv.order.delivery_address, custom_dest)

    def test_3_walkin_with_partner_and_valid_address(self):
        """3. Walk-in customer with partner and valid explicit address succeeds."""
        walkin_dest = "Shop #14, Street 2, Main Market"
        draft = DraftBillingService.create_draft(
            customer=None,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            delivery_address=walkin_dest,
            items_data=[{'product_id': self.samosa.id, 'quantity': 2, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )
        self.assertEqual(draft.delivery_address, walkin_dest)

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertEqual(inv.order.delivery_address, walkin_dest)

    def test_4_walkin_with_partner_and_missing_address_rejected(self):
        """4. Walk-in customer with partner and missing address raises ValidationError (server-side & API)."""
        with self.assertRaises(ValidationError) as ctx:
            DraftBillingService.create_draft(
                customer=None,
                billing_source='quick_billing',
                delivery_partner=self.partner,
                delivery_address='',
                items_data=[{'product_id': self.samosa.id, 'quantity': 2, 'unit_price': Decimal('15.00')}],
                created_by=self.owner,
            )
        self.assertIn('delivery_address', str(ctx.exception))

        # Test DRF API rejection
        res = self.client.post('/api/v1/billing/drafts/', {
            'billing_source': 'quick_billing',
            'delivery_partner_id': self.partner.id,
            'delivery_address': '',
            'items': [{'product_id': self.samosa.id, 'quantity': 2, 'unit_price': '15.00'}],
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('delivery_address', res.data)

    def test_5_walkin_without_partner_normal_counter_billing(self):
        """5. Walk-in customer without partner: address is optional and counter billing succeeds."""
        draft = DraftBillingService.create_draft(
            customer=None,
            billing_source='quick_billing',
            delivery_partner=None,
            items_data=[{'product_id': self.samosa.id, 'quantity': 2, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )
        self.assertEqual(draft.delivery_address, '')
        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertIsNone(inv.order)

    def test_6_registered_customer_without_partner(self):
        """6. Registered customer without partner: address is optional and finalizes without fulfillment order."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=None,
            items_data=[{'product_id': self.samosa.id, 'quantity': 1, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )
        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertIsNone(inv.order)

    def test_7_partner_removed_after_entering_address(self):
        """7. Partner removed via update retains or updates address cleanly."""
        draft = DraftBillingService.create_draft(
            customer=None,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            delivery_address="Somewhere in Sector 5",
            items_data=[{'product_id': self.samosa.id, 'quantity': 1, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )
        # Update draft to remove partner
        updated = DraftBillingService.update_draft(
            draft=draft,
            clear_delivery_partner=True,
        )
        self.assertIsNone(updated.delivery_partner)
        # Finalize now creates invoice without fulfillment order
        inv = DraftBillingService.finalize_draft(updated.id, self.owner)
        self.assertIsNone(inv.order)

    def test_8_existing_drafts_without_delivery_address_remain_compatible(self):
        """8. Existing drafts without delivery_address default to empty string and remain compatible."""
        draft = DraftInvoice.objects.create(
            draft_number='DRAFT-LEGACY-ADDR',
            customer=self.customer,
            subtotal=Decimal('30.00'),
            grand_total=Decimal('30.00'),
            status='draft',
        )
        self.assertEqual(draft.delivery_address, '')

        res = self.client.get(f'/api/v1/billing/drafts/{draft.id}/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['delivery_address'], '')

    def test_9_draft_edit_and_reload_preserves_address(self):
        """9. Editing draft via API PATCH preserves delivery_address on detail retrieval."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            items_data=[{'product_id': self.samosa.id, 'quantity': 1, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )

        res_patch = self.client.patch(f'/api/v1/billing/drafts/{draft.id}/', {
            'delivery_address': 'Building 9, Tech Park West'
        }, format='json')
        self.assertEqual(res_patch.status_code, status.HTTP_200_OK)
        self.assertEqual(res_patch.data['delivery_address'], 'Building 9, Tech Park West')

        res_get = self.client.get(f'/api/v1/billing/drafts/{draft.id}/')
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)
        self.assertEqual(res_get.data['delivery_address'], 'Building 9, Tech Park West')

    def test_10_finalized_order_receives_correct_destination(self):
        """10. Finalized fulfillment Order receives the exact destination specified on the draft."""
        dest = "Gate 2, Infotech Campus"
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            delivery_partner=self.partner,
            delivery_address=dest,
            items_data=[{'product_id': self.samosa.id, 'quantity': 3, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )

        inv = DraftBillingService.finalize_draft(draft.id, self.owner)
        self.assertEqual(inv.order.delivery_address, dest)
        self.assertEqual(inv.order.delivery.order.delivery_address, dest)
