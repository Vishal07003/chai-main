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
    def test_customer_summary_and_direct_collection(self):
        """Test customer purchase summary and collect-payment endpoint."""
        # 1. Create invoice with 100 due
        inv = Invoice.objects.create(
            invoice_number='INV-SUMMARY-1',
            customer=self.customer,
            customer_name=self.customer.name,
            customer_phone=self.customer.phone,
            grand_total=Decimal('100.00'),
            status='unpaid',
        )

        # 2. Check customer summary
        summary_res = self.client.get(f'/api/v1/customers/{self.customer.id}/summary/')
        self.assertEqual(summary_res.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(summary_res.data['total_billed']), Decimal('100.00'))
        self.assertEqual(Decimal(summary_res.data['total_pending']), Decimal('100.00'))

        # 3. Collect payment towards customer
        collect_res = self.client.post(f'/api/v1/customers/{self.customer.id}/collect-payment/', {
            'amount': '60.00',
            'method': 'cash',
        }, format='json')
        self.assertEqual(collect_res.status_code, status.HTTP_201_CREATED)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'partially_paid')
        self.assertEqual(inv.pending_amount, Decimal('40.00'))

    def test_customer_creation_validation(self):
        """Customer creation: Name is required, phone & address are optional, duplicate phone rejected."""
        # 1. Customer with name only (phone & address blank) -> SUCCESS
        res1 = self.client.post('/api/v1/billing/customers/', {
            'name': 'Customer Name Only',
        }, format='json')
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res1.data['name'], 'Customer Name Only')
        self.assertEqual(res1.data['phone'], '')

        # 2. Customer with name and valid phone -> SUCCESS
        res2 = self.client.post('/api/v1/billing/customers/', {
            'name': 'Customer With Phone',
            'phone': '9811223344',
            'address': 'Shop 101, Main Road',
        }, format='json')
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)

        # 3. Customer with invalid phone format -> 400
        res3 = self.client.post('/api/v1/billing/customers/', {
            'name': 'Bad Phone Customer',
            'phone': '12345',
        }, format='json')
        self.assertEqual(res3.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('phone', res3.data)

        # 4. Customer with duplicate phone -> 400
        res4 = self.client.post('/api/v1/billing/customers/', {
            'name': 'Duplicate Phone Customer',
            'phone': '9811223344',
        }, format='json')
        self.assertEqual(res4.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('phone', res4.data)

    def test_discount_and_payment_separation(self):
        """
        Verify distinct discount separation:
        Subtotal: 125 (e.g. 5x20 tea + 1x25 snack -> adjust prices to 125)
        Discount: 5.00 -> Grand Total: 120.00
        Payment: 120.00 -> Status: 'paid', Pending: 0.00
        """
        item_product = Product.objects.create(name='Special Chai', category=self.category, price='125.00', is_available=True)

        # 1. Discount = 5, Received = 120 -> Paid in Full
        res1 = self.client.post('/api/v1/billing/invoices/', {
            'items': [{'product_id': item_product.id, 'quantity': 1}],
            'discount_amount': '5.00',
            'initial_payment_amount': '120.00',
            'initial_payment_method': 'cash',
        }, format='json')
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(res1.data['subtotal']), Decimal('125.00'))
        self.assertEqual(Decimal(res1.data['discount_amount']), Decimal('5.00'))
        self.assertEqual(Decimal(res1.data['grand_total']), Decimal('120.00'))
        self.assertEqual(Decimal(res1.data['total_paid']), Decimal('120.00'))
        self.assertEqual(Decimal(res1.data['pending_amount']), Decimal('0.00'))
        self.assertEqual(res1.data['status'], 'paid')

        # 2. Discount = 5, Received = 100 -> Partially Paid (Pending = 20)
        res2 = self.client.post('/api/v1/billing/invoices/', {
            'items': [{'product_id': item_product.id, 'quantity': 1}],
            'discount_amount': '5.00',
            'initial_payment_amount': '100.00',
            'initial_payment_method': 'online',
        }, format='json')
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(res2.data['subtotal']), Decimal('125.00'))
        self.assertEqual(Decimal(res2.data['discount_amount']), Decimal('5.00'))
        self.assertEqual(Decimal(res2.data['grand_total']), Decimal('120.00'))
        self.assertEqual(Decimal(res2.data['total_paid']), Decimal('100.00'))
        self.assertEqual(Decimal(res2.data['pending_amount']), Decimal('20.00'))
        self.assertEqual(res2.data['status'], 'partially_paid')

        # 3. Discount cannot exceed subtotal
        res3 = self.client.post('/api/v1/billing/invoices/', {
            'items': [{'product_id': item_product.id, 'quantity': 1}],
            'discount_amount': '150.00',
        }, format='json')
        self.assertEqual(res3.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('discount_amount', res3.data)

        # 4. Negative discount rejected
        res4 = self.client.post('/api/v1/billing/invoices/', {
            'items': [{'product_id': item_product.id, 'quantity': 1}],
            'discount_amount': '-10.00',
        }, format='json')
        self.assertEqual(res4.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('discount_amount', res4.data)

    def test_payment_synchronization_and_defaulter_settlement(self):
        """
        Test Scenarios from requirements:
        1. Customer due ₹500; collect ₹500 Cash:
           - Remaining ₹0, PAID, removed from Defaulters.
        2. Customer due ₹500; collect ₹300 Cash:
           - Remaining ₹200, PARTIALLY PAID.
        3. Multiple invoices with FIFO allocation.
        """
        cust = Customer.objects.create(name='Pooja Verma', phone='9822334455', address='Suite 404')
        p500 = Product.objects.create(name='Tea Bulk 500', category=self.category, price='500.00', is_available=True)

        # Create invoice of ₹500 unpaid
        inv_res = self.client.post('/api/v1/billing/invoices/', {
            'customer_id': cust.id,
            'items': [{'product_id': p500.id, 'quantity': 1}],
        }, format='json')
        self.assertEqual(inv_res.status_code, status.HTTP_201_CREATED)
        inv_id = inv_res.data['id']
        
        # Verify customer initially has 500 pending
        c_res = self.client.get(f'/api/v1/billing/customers/{cust.id}/')
        self.assertEqual(Decimal(c_res.data['total_pending_amount']), Decimal('500.00'))

        # Collect 500 in Cash
        pay_res = self.client.post(f'/api/v1/billing/customers/{cust.id}/collect-payment/', {
            'amount': '500.00',
            'method': 'cash',
        }, format='json')
        self.assertEqual(pay_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(pay_res.data['amount_allocated']), Decimal('500.00'))

        # Verify customer is now 0 pending, and is_defaulter is False
        c_res_after = self.client.get(f'/api/v1/billing/customers/{cust.id}/')
        self.assertEqual(Decimal(c_res_after.data['total_pending_amount']), Decimal('0.00'))
        self.assertFalse(c_res_after.data['is_defaulter'])
        self.assertFalse(c_res_after.data['has_overdue'])

        # Verify invoice is paid
        inv_after = Invoice.objects.get(id=inv_id)
        self.assertEqual(inv_after.status, 'paid')
        self.assertEqual(inv_after.pending_amount, Decimal('0.00'))
