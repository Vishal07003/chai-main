from django.test import TestCase
from django.contrib.auth.models import User
from decimal import Decimal
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from products.models import Product, Category
from orders.models import Order, OrderItem
from billing.models import (
    Customer,
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


class Phase2DraftBillingServiceAndAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

        # 1. Users
        self.owner = User.objects.create_user(username='owner_phase2', password='Password123!', is_staff=True)
        self.non_owner = User.objects.create_user(username='non_owner', password='Password123!')
        from accounts.models import UserProfile
        UserProfile.objects.create(user=self.owner, phone='9888800001', role='owner')
        UserProfile.objects.create(user=self.non_owner, phone='9888800002', role='customer')

        # 2. Customers
        self.customer1 = Customer.objects.create(name='Pinnacle Solutions', phone='9876543210', address='Phase 1 SEZ')
        self.customer2 = Customer.objects.create(name='Global Apex', phone='9876543211', address='Phase 2 Tech Park')

        # 3. Products
        self.category = Category.objects.create(name='Hot Brews')
        self.tea = Product.objects.create(name='Special Chai', price=Decimal('15.00'), category=self.category)
        self.coffee = Product.objects.create(name='Filter Coffee', price=Decimal('25.00'), category=self.category)

        # 4. Orders for Customer 1
        self.order1 = Order.objects.create(
            customer=self.owner,
            customer_profile=self.customer1,
            status='delivered',
            total_amount=Decimal('45.00'),
            delivery_address='Building 4, SEZ'
        )
        OrderItem.objects.create(order=self.order1, product=self.tea, quantity=3, price=Decimal('15.00'))

        self.order2 = Order.objects.create(
            customer=self.owner,
            customer_profile=self.customer1,
            status='delivered',
            total_amount=Decimal('50.00'),
            delivery_address='Building 4, SEZ'
        )
        OrderItem.objects.create(order=self.order2, product=self.coffee, quantity=2, price=Decimal('25.00'))

        # 5. Order for Customer 2
        self.order3 = Order.objects.create(
            customer=self.owner,
            customer_profile=self.customer2,
            status='delivered',
            total_amount=Decimal('30.00'),
            delivery_address='Tech Park Tower'
        )
        OrderItem.objects.create(order=self.order3, product=self.tea, quantity=2, price=Decimal('15.00'))

    def test_unauthorized_access_rejected(self):
        """1. Unauthorized access (non-owner) to drafts API is rejected with 403 Forbidden"""
        self.client.force_authenticate(user=self.non_owner)
        res = self.client.get('/api/v1/billing/drafts/')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        res_post = self.client.post('/api/v1/billing/drafts/', {}, format='json')
        self.assertEqual(res_post.status_code, status.HTTP_403_FORBIDDEN)

    def test_quick_billing_draft_creation_api(self):
        """2. POST /api/v1/billing/drafts/ for Quick Billing creates draft and items cleanly"""
        self.client.force_authenticate(user=self.owner)
        payload = {
            'customer_id': self.customer1.id,
            'billing_source': 'quick_billing',
            'discount_amount': '5.00',
            'due_date': '2026-11-25',
            'notes': 'Stall morning counter draft',
            'items': [
                {'product_id': self.tea.id, 'product_name': self.tea.name, 'unit': 'Cup', 'quantity': 10, 'unit_price': '15.00'},
                {'product_id': self.coffee.id, 'product_name': self.coffee.name, 'unit': 'Cup', 'quantity': 4, 'unit_price': '25.00'},
            ]
        }
        res = self.client.post('/api/v1/billing/drafts/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.data
        self.assertTrue(data['draft_number'].startswith('DRAFT-'))
        self.assertEqual(data['status'], 'draft')
        self.assertEqual(data['billing_source'], 'quick_billing')
        self.assertEqual(data['subtotal'], '250.00')  # 150 + 100
        self.assertEqual(data['discount_amount'], '5.00')
        self.assertEqual(data['grand_total'], '245.00')
        self.assertEqual(data['due_date'], '2026-11-25')
        self.assertEqual(len(data['items']), 2)

        # Official sales & invoices must remain 0
        self.assertEqual(Invoice.objects.count(), 0)

    def test_single_order_draft_creation_api(self):
        """3. Single order billing draft populates from order items"""
        self.client.force_authenticate(user=self.owner)
        payload = {
            'billing_source': 'single_order',
            'order_ids': [self.order1.id],
            'notes': 'Single order draft',
        }
        res = self.client.post('/api/v1/billing/drafts/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.data
        self.assertEqual(data['customer'], self.customer1.id)
        self.assertEqual(data['subtotal'], '45.00')
        self.assertEqual(len(data['draft_orders']), 1)
        self.assertEqual(data['draft_orders'][0]['order_id'], self.order1.id)

    def test_multiple_order_consolidated_draft_creation_api(self):
        """4. Consolidated orders draft attaches multiple verified orders of same customer"""
        self.client.force_authenticate(user=self.owner)
        payload = {
            'billing_source': 'consolidated_orders',
            'order_ids': [self.order1.id, self.order2.id],
            'discount_amount': '10.00',
        }
        res = self.client.post('/api/v1/billing/drafts/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.data
        self.assertEqual(data['subtotal'], '95.00')  # 45 + 50
        self.assertEqual(data['discount_amount'], '10.00')
        self.assertEqual(data['grand_total'], '85.00')
        self.assertEqual(len(data['draft_orders']), 2)

    def test_draft_editing_and_recalculation_api(self):
        """5. PATCH /api/v1/billing/drafts/{id}/ updates items, discount, due_date and recalculates totals"""
        draft = DraftBillingService.create_draft(
            customer=self.customer1,
            billing_source='quick_billing',
            items_data=[{'product_id': self.tea.id, 'quantity': 2, 'unit_price': Decimal('15.00')}]
        )
        self.assertEqual(draft.grand_total, Decimal('30.00'))

        self.client.force_authenticate(user=self.owner)
        patch_payload = {
            'discount_amount': '6.00',
            'due_date': '2026-11-30',
            'notes': 'Updated draft notes',
            'items': [
                {'product_id': self.tea.id, 'quantity': 4, 'unit_price': '15.00'},  # 60
                {'product_id': self.coffee.id, 'quantity': 2, 'unit_price': '25.00'},  # 50 -> 110 - 6 = 104
            ]
        }
        res = self.client.patch(f'/api/v1/billing/drafts/{draft.id}/', patch_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.data
        self.assertEqual(data['subtotal'], '110.00')
        self.assertEqual(data['discount_amount'], '6.00')
        self.assertEqual(data['grand_total'], '104.00')
        self.assertEqual(data['due_date'], '2026-11-30')
        self.assertEqual(data['notes'], 'Updated draft notes')

    def test_draft_listing_and_filtering(self):
        """6. GET /api/v1/billing/drafts/ supports status, customer, and billing_source filters"""
        d1 = DraftBillingService.create_draft(customer=self.customer1, billing_source='quick_billing', items_data=[{'quantity': 1, 'unit_price': 10}])
        d2 = DraftBillingService.create_draft(customer=self.customer2, billing_source='single_order', order_ids=[self.order3.id])

        self.client.force_authenticate(user=self.owner)
        res_all = self.client.get('/api/v1/billing/drafts/')
        self.assertEqual(len(res_all.data), 2)

        res_cust1 = self.client.get(f'/api/v1/billing/drafts/?customer={self.customer1.id}')
        self.assertEqual(len(res_cust1.data), 1)
        self.assertEqual(res_cust1.data[0]['id'], d1.id)

        res_source = self.client.get('/api/v1/billing/drafts/?billing_source=single_order')
        self.assertEqual(len(res_source.data), 1)
        self.assertEqual(res_source.data[0]['id'], d2.id)

    def test_draft_cancellation_releases_reservations_api(self):
        """7. POST /api/v1/billing/drafts/{id}/cancel/ marks cancelled and releases order reservations"""
        draft = DraftBillingService.create_draft(
            customer=self.customer1,
            billing_source='single_order',
            order_ids=[self.order1.id]
        )

        self.client.force_authenticate(user=self.owner)
        res_cancel = self.client.post(f'/api/v1/billing/drafts/{draft.id}/cancel/')
        self.assertEqual(res_cancel.status_code, status.HTTP_200_OK)
        self.assertEqual(res_cancel.data['status'], 'cancelled')

        # Order 1 can now be drafted again without conflict
        res_new = self.client.post('/api/v1/billing/drafts/', {
            'billing_source': 'single_order',
            'order_ids': [self.order1.id]
        }, format='json')
        self.assertEqual(res_new.status_code, status.HTTP_201_CREATED)

    def test_atomic_draft_finalization_api(self):
        """8. POST /api/v1/billing/drafts/{id}/finalize/ creates official Invoice, InvoiceItems, InvoiceOrders"""
        draft = DraftBillingService.create_draft(
            customer=self.customer1,
            billing_source='consolidated_orders',
            order_ids=[self.order1.id, self.order2.id],
            discount_amount=Decimal('5.00'),
            due_date=timezone.now().date()
        )

        self.client.force_authenticate(user=self.owner)
        res_fin = self.client.post(f'/api/v1/billing/drafts/{draft.id}/finalize/', {'notes': 'Official batch bill'}, format='json')
        self.assertEqual(res_fin.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res_fin.data['success'])

        inv_data = res_fin.data['invoice']
        self.assertTrue(inv_data['invoice_number'].startswith('INV-'))
        self.assertEqual(inv_data['grand_total'], '90.00')  # 95 - 5

        # Verify DB records
        official_inv = Invoice.objects.get(invoice_number=inv_data['invoice_number'])
        self.assertEqual(official_inv.status, 'unpaid')
        self.assertEqual(official_inv.items.count(), 2)
        self.assertEqual(InvoiceOrder.objects.filter(invoice=official_inv).count(), 2)

        # Verify draft state
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'finalized')
        self.assertEqual(draft.official_invoice, official_inv)

        # Orders must now show has_invoice = True
        self.order1.refresh_from_db()
        self.order2.refresh_from_db()
        self.assertTrue(self.order1.has_invoice)
        self.assertTrue(self.order2.has_invoice)

    def test_duplicate_finalization_rejected(self):
        """9. Duplicate finalization attempts on already finalized drafts are rejected"""
        draft = DraftBillingService.create_draft(
            customer=self.customer1,
            billing_source='quick_billing',
            items_data=[{'product_id': self.tea.id, 'quantity': 1, 'unit_price': Decimal('15.00')}]
        )
        self.client.force_authenticate(user=self.owner)

        # First finalization succeeds
        res1 = self.client.post(f'/api/v1/billing/drafts/{draft.id}/finalize/', {}, format='json')
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        # Second finalization must be rejected
        res2 = self.client.post(f'/api/v1/billing/drafts/{draft.id}/finalize/', {}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('cannot be finalized', str(res2.data['detail']))

    def test_cross_relationship_duplicate_order_invoicing_rejected(self):
        """10. Cannot finalize a draft if one of its orders was already invoiced elsewhere"""
        # Officially invoice order 1 directly
        legacy_inv = Invoice.objects.create(
            invoice_number='INV-20261120-9999',
            order=self.order1,
            customer=self.customer1,
            subtotal=Decimal('45.00'),
            grand_total=Decimal('45.00')
        )

        draft = DraftInvoice.objects.create(
            draft_number='DRAFT-TEST-FAIL',
            customer=self.customer1,
            billing_source='single_order',
            status='draft'
        )
        DraftInvoiceOrder.objects.create(draft=draft, order=self.order1)
        DraftInvoiceItem.objects.create(draft=draft, product=self.tea, product_name='Tea', quantity=1, unit_price=Decimal('15.00'), total_price=Decimal('15.00'))

        self.client.force_authenticate(user=self.owner)
        res_fin = self.client.post(f'/api/v1/billing/drafts/{draft.id}/finalize/', {}, format='json')
        self.assertEqual(res_fin.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('already been officially invoiced', str(res_fin.data))
