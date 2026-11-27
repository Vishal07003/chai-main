from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from billing.models import Customer, Company, Invoice, InvoiceItem
from products.models import Product, Category
from orders.models import Order

User = get_user_model()


class RegisteredCustomerMandatoryTests(TestCase):
    """
    Tests enforcing registered customer for all new orders and invoices,
    and verifying historical Walk-in invoice retrieval remains intact.
    """

    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username='owner_user',
            password='password123',
            is_staff=True,
            is_superuser=True
        )
        self.client.force_authenticate(user=self.owner)

        self.category = Category.objects.create(name='Beverages')
        self.chai = Product.objects.create(
            name='Special Masala Chai',
            category=self.category,
            price=Decimal('20.00'),
            is_available=True
        )

        self.customer = Customer.objects.create(
            name='Rajesh Kumar',
            phone='9876543210',
            address='Shop 42, Main Market'
        )

        self.company = Company.objects.create(
            name='Tech Corp Ltd',
            phone='9123456780',
            billing_address='Tech Park, Floor 3'
        )

        # Create historical walk-in invoice (no customer FK, customer_name='Walk-in Customer')
        self.historical_invoice = Invoice.objects.create(
            invoice_number='INV-HISTORICAL-001',
            customer=None,
            customer_name='Walk-in Customer',
            customer_phone='',
            subtotal=Decimal('40.00'),
            discount_amount=Decimal('0.00'),
            grand_total=Decimal('40.00'),
            status='paid'
        )
        InvoiceItem.objects.create(
            invoice=self.historical_invoice,
            product=self.chai,
            product_name=self.chai.name,
            quantity=2,
            unit_price=Decimal('20.00'),
            total_price=Decimal('40.00')
        )

    def test_1_no_customer_and_no_company_invoice_rejected(self):
        """1. Invoice creation with no customer and no company must be rejected."""
        payload = {
            'items': [{'product_id': self.chai.id, 'quantity': 2}],
        }
        res = self.client.post('/api/v1/billing/invoices/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('customer_id', res.data)

    def test_2_no_customer_with_valid_company_invoice_rejected(self):
        """2. Invoice creation with valid company but NO customer must still be rejected (company cannot bypass customer)."""
        payload = {
            'company_id': self.company.id,
            'items': [{'product_id': self.chai.id, 'quantity': 2}],
        }
        res = self.client.post('/api/v1/billing/invoices/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('customer_id', res.data)

    def test_3_valid_customer_invoice_accepted(self):
        """3. Invoice creation with valid customer must be accepted."""
        payload = {
            'customer_id': self.customer.id,
            'items': [{'product_id': self.chai.id, 'quantity': 3}],
        }
        res = self.client.post('/api/v1/billing/invoices/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['customer'], self.customer.id)
        self.assertEqual(res.data['customer_name'], self.customer.name)
        self.assertEqual(Decimal(res.data['grand_total']), Decimal('60.00'))

    def test_4_valid_customer_and_valid_company_invoice_accepted(self):
        """4. Invoice creation with valid customer AND valid company metadata must be accepted."""
        payload = {
            'customer_id': self.customer.id,
            'company_id': self.company.id,
            'items': [{'product_id': self.chai.id, 'quantity': 1}],
        }
        res = self.client.post('/api/v1/billing/invoices/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['customer'], self.customer.id)
        self.assertEqual(res.data['company'], self.company.id)

    def test_5_invalid_customer_id_invoice_rejected(self):
        """5. Invoice creation with invalid/nonexistent customer ID must be rejected."""
        payload = {
            'customer_id': 999999,
            'items': [{'product_id': self.chai.id, 'quantity': 1}],
        }
        res = self.client.post('/api/v1/billing/invoices/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('customer_id', res.data)

    def test_6_order_creation_without_customer_rejected(self):
        """6. Order creation without customer (even with company) must be rejected."""
        payload_no_cust = {
            'company_id': self.company.id,
            'delivery_address': 'Shop 10',
            'items': [{'product_id': self.chai.id, 'quantity': 2}],
        }
        res = self.client.post('/api/v1/orders/', payload_no_cust, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('customer_id', res.data)

    def test_7_order_creation_with_valid_customer_accepted(self):
        """7. Order creation with valid customer must be accepted."""
        payload = {
            'customer_id': self.customer.id,
            'company_id': self.company.id,
            'delivery_address': self.customer.address,
            'items': [{'product_id': self.chai.id, 'quantity': 2}],
        }
        res = self.client.post('/api/v1/orders/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get(id=res.data['id'])
        self.assertEqual(order.customer_profile, self.customer)
        self.assertEqual(order.company, self.company)

    def test_8_historical_walkin_invoice_retrieval_and_details_still_work(self):
        """8. Historical walk-in invoices must continue to load without errors."""
        res = self.client.get(f'/api/v1/billing/invoices/{self.historical_invoice.id}/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['invoice_number'], 'INV-HISTORICAL-001')
        self.assertEqual(res.data['customer_name'], 'Walk-in Customer')
        self.assertIsNone(res.data['customer'])
        self.assertEqual(Decimal(res.data['grand_total']), Decimal('40.00'))
