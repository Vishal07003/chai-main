from django.test import TestCase
from django.contrib.auth.models import User
from decimal import Decimal
from rest_framework.test import APIClient
from rest_framework import status
from accounts.models import UserProfile
from products.models import Product, Category
from billing.models import (
    Customer,
    Invoice,
    InvoiceItem,
    DraftInvoice,
    DraftInvoiceItem,
    Expense,
)
from billing.draft_service import DraftBillingService


class PhaseR1FinancialDataTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='owner1', password='pass', email='o@c.com')
        self.owner_profile = UserProfile.objects.create(user=self.owner, phone='9988771122', role='owner')

        self.staff = User.objects.create_user(username='delivery1', password='pass', email='d@c.com')
        self.staff_profile = UserProfile.objects.create(user=self.staff, phone='9876500001', role='delivery')

        self.category = Category.objects.create(name='Beverages')
        self.product = Product.objects.create(
            name='Ginger Tea',
            category=self.category,
            price=Decimal('25.00'),
            cost_price=Decimal('10.00'),
            unit='Cup',
        )

        self.customer = Customer.objects.create(
            name='Karan Johar',
            phone='9876543210',
            address='Mumbai Office',
        )

        self.client = APIClient()

    def test_product_cost_price_crud_and_validation(self):
        """Test Product cost_price field can be configured and validates non-negative amount."""
        self.client.force_authenticate(user=self.owner)

        # Update product with cost price
        resp = self.client.patch(f'/api/v1/products/items/{self.product.id}/', {
            'cost_price': '12.50'
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_price, Decimal('12.50'))
        # Selling price unchanged
        self.assertEqual(self.product.price, Decimal('25.00'))

        # Negative cost price validation
        bad_resp = self.client.patch(f'/api/v1/products/items/{self.product.id}/', {
            'cost_price': '-5.00'
        }, format='json')
        self.assertEqual(bad_resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_draft_cost_snapshot_creation_and_update(self):
        """Test draft items capture unit_cost and total_cost and recalculate on quantity update."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            items_data=[
                {'product_id': self.product.id, 'quantity': 3, 'unit_price': '25.00'}
            ],
            created_by=self.owner,
        )

        item = draft.items.first()
        self.assertEqual(item.unit_cost, Decimal('10.00'))
        self.assertEqual(item.total_cost, Decimal('30.00'))
        self.assertEqual(item.total_price, Decimal('75.00'))

        # Update draft with new quantity and custom cost
        updated_draft = DraftBillingService.update_draft(
            draft=draft,
            items_data=[
                {'product_id': self.product.id, 'quantity': 5, 'unit_price': '25.00', 'unit_cost': '11.00'}
            ]
        )
        upd_item = updated_draft.items.first()
        self.assertEqual(upd_item.quantity, 5)
        self.assertEqual(upd_item.unit_cost, Decimal('11.00'))
        self.assertEqual(upd_item.total_cost, Decimal('55.00'))
        self.assertEqual(upd_item.total_price, Decimal('125.00'))

    def test_invoice_finalization_cost_snapshot_and_subsequent_product_cost_mutation(self):
        """Finalized InvoiceItem locks cost snapshot; subsequent Product changes never alter historical snapshot."""
        draft = DraftBillingService.create_draft(
            customer=self.customer,
            billing_source='quick_billing',
            items_data=[
                {'product_id': self.product.id, 'quantity': 4, 'unit_price': '25.00'}
            ],
            created_by=self.owner,
        )

        official_invoice = DraftBillingService.finalize_draft(
            draft_id=draft.id,
            owner_user=self.owner,
        )

        inv_item = official_invoice.items.first()
        self.assertEqual(inv_item.unit_cost, Decimal('10.00'))
        self.assertEqual(inv_item.total_cost, Decimal('40.00'))
        self.assertTrue(inv_item.has_cost_snapshot)

        # Mutate product cost price
        self.product.cost_price = Decimal('18.00')
        self.product.price = Decimal('35.00')
        self.product.save()

        # Historical invoice item cost MUST remain completely unchanged
        inv_item.refresh_from_db()
        self.assertEqual(inv_item.unit_cost, Decimal('10.00'))
        self.assertEqual(inv_item.total_cost, Decimal('40.00'))
        self.assertEqual(inv_item.unit_price, Decimal('25.00'))

    def test_missing_historical_cost_preserves_none(self):
        """Historical items created before cost tracking retain null cost snapshots without invented zero costs."""
        legacy_invoice = Invoice.objects.create(
            invoice_number='INV-LEGACY-001',
            customer=self.customer,
            customer_name=self.customer.name,
            subtotal=Decimal('50.00'),
            grand_total=Decimal('50.00'),
            status='paid',
        )
        legacy_item = InvoiceItem.objects.create(
            invoice=legacy_invoice,
            product=self.product,
            product_name='Old Tea',
            unit_price=Decimal('25.00'),
            quantity=2,
            total_price=Decimal('50.00'),
            # unit_cost and total_cost are None
        )

        legacy_item.refresh_from_db()
        self.assertIsNone(legacy_item.unit_cost)
        self.assertIsNone(legacy_item.total_cost)
        self.assertFalse(legacy_item.has_cost_snapshot)

    def test_expense_crud_owner_permissions_and_validation(self):
        """Test Expense model owner CRUD, filtering and non-owner access denial."""
        # 1. Non-owner blocked
        self.client.force_authenticate(user=self.staff)
        resp_denied = self.client.post('/api/v1/billing/expenses/', {
            'title': 'Shop Rent',
            'category': 'rent',
            'amount': '15000.00',
            'expense_date': '2026-11-20',
        }, format='json')
        self.assertEqual(resp_denied.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Owner can create
        self.client.force_authenticate(user=self.owner)
        resp_create = self.client.post('/api/v1/billing/expenses/', {
            'title': 'November Shop Rent',
            'category': 'rent',
            'amount': '15000.00',
            'expense_date': '2026-11-20',
            'payment_method': 'bank_transfer',
            'notes': 'Paid via HDFC NEFT'
        }, format='json')
        self.assertEqual(resp_create.status_code, status.HTTP_201_CREATED)
        expense_id = resp_create.data['id']
        self.assertEqual(resp_create.data['category_display'], 'Rent')
        self.assertEqual(resp_create.data['recorded_by_name'], 'owner1')

        # 3. Validation: Amount > 0
        resp_bad = self.client.post('/api/v1/billing/expenses/', {
            'title': 'Negative expense',
            'category': 'transport',
            'amount': '-100.00',
        }, format='json')
        self.assertEqual(resp_bad.status_code, status.HTTP_400_BAD_REQUEST)

        # 4. List and filter
        resp_list = self.client.get('/api/v1/billing/expenses/?category=rent')
        self.assertEqual(resp_list.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp_list.data), 1)

        # 5. Update
        resp_upd = self.client.patch(f'/api/v1/billing/expenses/{expense_id}/', {
            'amount': '16000.00'
        }, format='json')
        self.assertEqual(resp_upd.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(str(resp_upd.data['amount'])), Decimal('16000.00'))

        # 6. Delete
        resp_del = self.client.delete(f'/api/v1/billing/expenses/{expense_id}/')
        self.assertEqual(resp_del.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Expense.objects.filter(id=expense_id).exists())
