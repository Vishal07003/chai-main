from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import UserProfile
from products.models import Category, Product
from deliveries.models import Delivery, DeliveryPartner
from orders.models import Order
from billing.models import Customer, DraftInvoice, Invoice
from billing.draft_service import DraftBillingService


class Phase4A2DeliveryPartnerDraftSupportTests(TestCase):
    """
    Phase 4A.2 Verification Tests:
    1. Draft created without a partner.
    2. Draft created with a valid partner.
    3. Draft updated with a different partner.
    4. Partner removed using null.
    5. Invalid partner rejected.
    6. Inactive partner rejected.
    7. Draft cancellation creates no delivery assignment.
    8. Draft creation creates no Order or Delivery.
    9. Finalization does not create duplicate orders or deliveries.
    10. Existing drafts without partners remain readable and editable.
    """

    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(username='partner_owner', password='Password123!')
        self.owner_profile = UserProfile.objects.create(user=self.owner, phone='9988771122', role='owner')
        self.client.force_authenticate(user=self.owner)

        self.category = Category.objects.create(name='Snacks')
        self.samosa = Product.objects.create(name='Samosa', category=self.category, price='15.00', is_available=True)
        self.customer = Customer.objects.create(name='Vikram Singh', phone='9811002233')

        # Active Partner 1
        self.partner_user1 = User.objects.create_user(username='driver_rajesh', password='Password123!')
        UserProfile.objects.create(user=self.partner_user1, phone='9876500001', role='delivery')
        self.partner1 = DeliveryPartner.objects.create(
            user=self.partner_user1,
            name='Rajesh Kumar',
            phone='9876500001',
            is_active=True,
            status='READY'
        )

        # Active Partner 2
        self.partner_user2 = User.objects.create_user(username='driver_suresh', password='Password123!')
        UserProfile.objects.create(user=self.partner_user2, phone='9876500002', role='delivery')
        self.partner2 = DeliveryPartner.objects.create(
            user=self.partner_user2,
            name='Suresh Patel',
            phone='9876500002',
            is_active=True,
            status='READY'
        )

        # Inactive Partner
        self.partner_user_inact = User.objects.create_user(username='driver_inactive', password='Password123!')
        UserProfile.objects.create(user=self.partner_user_inact, phone='9876500003', role='delivery')
        self.inactive_partner = DeliveryPartner.objects.create(
            user=self.partner_user_inact,
            name='Inactive Driver',
            phone='9876500003',
            is_active=False,
            status='OFFLINE'
        )

    def test_1_draft_created_without_partner(self):
        """1. Draft can be created without a delivery partner."""
        res = self.client.post('/api/v1/billing/drafts/', {
            'customer_id': self.customer.id,
            'billing_source': 'quick_billing',
            'items': [{'product_id': self.samosa.id, 'quantity': 2, 'unit_price': '15.00'}],
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(res.data['delivery_partner'])
        self.assertIsNone(res.data['delivery_partner_id'])
        self.assertIsNone(res.data['delivery_partner_name'])

    def test_2_draft_created_with_valid_partner(self):
        """2. Draft created with a valid active delivery partner stores partner reference."""
        res = self.client.post('/api/v1/billing/drafts/', {
            'customer_id': self.customer.id,
            'delivery_partner_id': self.partner1.id,
            'billing_source': 'quick_billing',
            'items': [{'product_id': self.samosa.id, 'quantity': 3, 'unit_price': '15.00'}],
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['delivery_partner_id'], self.partner1.id)
        self.assertEqual(res.data['delivery_partner_name'], 'Rajesh Kumar')

        draft = DraftInvoice.objects.get(id=res.data['id'])
        self.assertEqual(draft.delivery_partner, self.partner1)

    def test_3_draft_updated_with_different_partner(self):
        """3. Draft partner can be modified to another active partner via PATCH."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            delivery_partner=self.partner1,
            items_data=[{'product_name': 'Samosa', 'quantity': 1, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )
        self.assertEqual(draft.delivery_partner, self.partner1)

        res = self.client.patch(f'/api/v1/billing/drafts/{draft.id}/', {
            'delivery_partner_id': self.partner2.id
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['delivery_partner_id'], self.partner2.id)
        self.assertEqual(res.data['delivery_partner_name'], 'Suresh Patel')

        draft.refresh_from_db()
        self.assertEqual(draft.delivery_partner, self.partner2)

    def test_4_partner_removed_using_null(self):
        """4. Partner can be cleared from draft by sending null."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            delivery_partner=self.partner1,
            items_data=[{'product_name': 'Samosa', 'quantity': 1, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )
        res = self.client.patch(f'/api/v1/billing/drafts/{draft.id}/', {
            'delivery_partner_id': None
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIsNone(res.data['delivery_partner_id'])
        self.assertIsNone(res.data['delivery_partner_name'])

        draft.refresh_from_db()
        self.assertIsNone(draft.delivery_partner)

    def test_5_invalid_partner_rejected(self):
        """5. Non-existent partner ID is rejected with 400."""
        res = self.client.post('/api/v1/billing/drafts/', {
            'customer_id': self.customer.id,
            'delivery_partner_id': 999999,
            'items': [{'product_id': self.samosa.id, 'quantity': 1, 'unit_price': '15.00'}],
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('delivery_partner_id', res.data)

    def test_6_inactive_partner_rejected(self):
        """6. Inactive partner is rejected with 400 validation error."""
        res = self.client.post('/api/v1/billing/drafts/', {
            'customer_id': self.customer.id,
            'delivery_partner_id': self.inactive_partner.id,
            'items': [{'product_id': self.samosa.id, 'quantity': 1, 'unit_price': '15.00'}],
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('delivery_partner_id', res.data)

    def test_7_draft_creation_creates_no_order_or_delivery(self):
        """7. Creating a draft with delivery partner does NOT create an Order or Delivery record."""
        order_count_before = Order.objects.count()
        delivery_count_before = Delivery.objects.count()

        res = self.client.post('/api/v1/billing/drafts/', {
            'customer_id': self.customer.id,
            'delivery_partner_id': self.partner1.id,
            'items': [{'product_id': self.samosa.id, 'quantity': 5, 'unit_price': '15.00'}],
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        self.assertEqual(Order.objects.count(), order_count_before)
        self.assertEqual(Delivery.objects.count(), delivery_count_before)

    def test_8_draft_cancellation_creates_no_delivery_assignment(self):
        """8. Cancelling a draft creates no deliveries and cleanly updates draft status."""
        delivery_count_before = Delivery.objects.count()
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            delivery_partner=self.partner1,
            items_data=[{'product_name': 'Samosa', 'quantity': 1, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )

        res = self.client.post(f'/api/v1/billing/drafts/{draft.id}/cancel/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['status'], 'cancelled')
        self.assertEqual(Delivery.objects.count(), delivery_count_before)

    def test_9_finalization_creates_no_duplicate_orders_or_deliveries(self):
        """9. Finalizing a draft with a delivery partner generates an official invoice and exactly 1 fulfillment order and delivery."""
        order_count_before = Order.objects.count()
        delivery_count_before = Delivery.objects.count()

        draft = DraftBillingService.create_draft(
            customer=self.customer,
            delivery_partner=self.partner1,
            items_data=[{'product_name': 'Samosa', 'quantity': 2, 'unit_price': Decimal('15.00')}],
            created_by=self.owner,
        )

        res = self.client.post(f'/api/v1/billing/drafts/{draft.id}/finalize/', {'notes': 'Official bill'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        # Official invoice created
        self.assertTrue(Invoice.objects.filter(invoice_number=res.data['invoice']['invoice_number']).exists())
        # Phase 5B: Exactly 1 fulfillment Order and 1 Delivery created
        self.assertEqual(Order.objects.count(), order_count_before + 1)
        self.assertEqual(Delivery.objects.count(), delivery_count_before + 1)

    def test_10_existing_drafts_without_partner_remain_valid(self):
        """10. Existing drafts with null delivery_partner remain readable and editable."""
        draft = DraftInvoice.objects.create(
            draft_number='DRAFT-LEGACY-001',
            customer=self.customer,
            subtotal=Decimal('30.00'),
            grand_total=Decimal('30.00'),
            status='draft',
            delivery_partner=None,
        )

        res = self.client.get(f'/api/v1/billing/drafts/{draft.id}/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIsNone(res.data['delivery_partner_id'])

        # Now assign partner
        res_patch = self.client.patch(f'/api/v1/billing/drafts/{draft.id}/', {
            'delivery_partner_id': self.partner1.id
        }, format='json')
        self.assertEqual(res_patch.status_code, status.HTTP_200_OK)
        self.assertEqual(res_patch.data['delivery_partner_id'], self.partner1.id)
