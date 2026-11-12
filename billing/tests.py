from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from decimal import Decimal
from accounts.models import UserProfile
from products.models import Category, Product
from billing.models import Customer, Invoice, Payment

class BillingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(username='chaiowner', password='Password123!')
        UserProfile.objects.create(user=self.owner, phone='1111111111', role='owner')
        self.client.force_authenticate(user=self.owner)

        self.category = Category.objects.create(name='Hot Chai')
        self.tea = Product.objects.create(name='Ginger Chai', category=self.category, price='20.00', is_available=True)
        self.snack = Product.objects.create(name='Bun Maska', category=self.category, price='30.00', is_available=True)

        self.customer = Customer.objects.create(name='Ramesh Kumar', phone='9876543210', address='Shop 12')

    def test_create_walkin_quick_bill_full_cash(self):
        """Walk-in bill without customer, fully paid by cash."""
        data = {
            'items': [
                {'product_id': self.tea.id, 'quantity': 2},    # 20 * 2 = 40
                {'product_id': self.snack.id, 'quantity': 1},  # 30 * 1 = 30 -> Total 70.00
            ],
            'initial_payment_amount': '70.00',
            'initial_payment_method': 'cash',
        }
        res = self.client.post('/api/v1/billing/invoices/', data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['customer_name'], 'Walk-in Customer')
        self.assertEqual(Decimal(res.data['grand_total']), Decimal('70.00'))
        self.assertEqual(Decimal(res.data['total_paid']), Decimal('70.00'))
        self.assertEqual(Decimal(res.data['pending_amount']), Decimal('0.00'))
        self.assertEqual(res.data['status'], 'paid')

    def test_create_customer_partial_payment_and_collect_later(self):
        """Bill linked to customer, partially paid by online method, then remainder paid later."""
        data = {
            'customer_id': self.customer.id,
            'items': [
                {'product_id': self.tea.id, 'quantity': 5},  # 20 * 5 = 100.00
            ],
            'initial_payment_amount': '40.00',
            'initial_payment_method': 'online',
        }
        res = self.client.post('/api/v1/billing/invoices/', data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        inv_id = res.data['id']
        self.assertEqual(res.data['status'], 'partially_paid')
        self.assertEqual(Decimal(res.data['pending_amount']), Decimal('60.00'))

        # Later: Add payment of ₹60 via cash
        pay_res = self.client.post(f'/api/v1/billing/invoices/{inv_id}/payments/', {
            'amount': '60.00',
            'method': 'cash',
            'notes': 'Remaining cleared'
        }, format='json')
        self.assertEqual(pay_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(pay_res.data['status'], 'paid')
        self.assertEqual(Decimal(pay_res.data['pending_amount']), Decimal('0.00'))
        self.assertEqual(Decimal(pay_res.data['total_paid']), Decimal('100.00'))

    def test_payment_cannot_exceed_pending_amount(self):
        """Validation: payment cannot exceed the remaining balance."""
        data = {
            'customer_id': self.customer.id,
            'items': [{'product_id': self.tea.id, 'quantity': 1}],  # 20.00
            'initial_payment_amount': '0.00',
        }
        res = self.client.post('/api/v1/billing/invoices/', data, format='json')
        inv_id = res.data['id']

        # Attempt to pay ₹50 on a ₹20 bill
        pay_res = self.client.post(f'/api/v1/billing/invoices/{inv_id}/payments/', {
            'amount': '50.00',
            'method': 'cash',
        }, format='json')
        self.assertEqual(pay_res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_invoice_history_endpoint(self):
        """Owner can view customer history with aggregate metrics."""
        # Create 2 invoices for Ramesh
        self.client.post('/api/v1/billing/invoices/', {
            'customer_id': self.customer.id,
            'items': [{'product_id': self.tea.id, 'quantity': 2}],  # 40
            'initial_payment_amount': '40.00',
        }, format='json')

        self.client.post('/api/v1/billing/invoices/', {
            'customer_id': self.customer.id,
            'items': [{'product_id': self.snack.id, 'quantity': 2}], # 60
            'initial_payment_amount': '20.00',
        }, format='json')

        # Check customer list aggregation
        cust_res = self.client.get('/api/v1/billing/customers/')
        self.assertEqual(cust_res.status_code, status.HTTP_200_OK)
        ramesh = next(c for c in cust_res.data if c['id'] == self.customer.id)
        self.assertEqual(ramesh['total_bills'], 2)
        self.assertEqual(Decimal(ramesh['total_billed_amount']), Decimal('100.00'))
        self.assertEqual(Decimal(ramesh['total_pending_amount']), Decimal('40.00'))

        # Check customer invoices history
        hist_res = self.client.get(f'/api/v1/billing/customers/{self.customer.id}/invoices/')
        self.assertEqual(hist_res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(hist_res.data), 2)

    def test_dashboard_stats_endpoint(self):
        """Dashboard calculates total sales, cash received, online received and pending."""
        # Create bill 1: 50.00 (paid 50 cash)
        self.client.post('/api/v1/billing/invoices/', {
            'items': [{'product_id': self.tea.id, 'quantity': 1}, {'product_id': self.snack.id, 'quantity': 1}],
            'initial_payment_amount': '50.00',
            'initial_payment_method': 'cash',
        }, format='json')

        # Create bill 2: 40.00 (paid 20 online, 20 pending)
        self.client.post('/api/v1/billing/invoices/', {
            'items': [{'product_id': self.tea.id, 'quantity': 2}],
            'initial_payment_amount': '20.00',
            'initial_payment_method': 'online',
        }, format='json')

        stats_res = self.client.get('/api/v1/billing/invoices/dashboard_stats/')
        self.assertEqual(stats_res.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(stats_res.data['total_sales']), Decimal('90.00'))
        self.assertEqual(Decimal(stats_res.data['cash_received']), Decimal('50.00'))
        self.assertEqual(Decimal(stats_res.data['online_received']), Decimal('20.00'))
        self.assertEqual(Decimal(stats_res.data['total_received']), Decimal('70.00'))
        self.assertEqual(Decimal(stats_res.data['total_pending']), Decimal('20.00'))
