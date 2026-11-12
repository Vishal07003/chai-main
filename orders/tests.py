from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from accounts.models import UserProfile
from products.models import Category, Product
from orders.models import Order

class OrdersTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(username='chaiowner', password='Password123!')
        UserProfile.objects.create(user=self.owner, phone='1111111111', role='owner')

        self.customer1 = User.objects.create_user(username='customer1', password='Password123!')
        UserProfile.objects.create(user=self.customer1, phone='2222222222', role='customer')

        self.customer2 = User.objects.create_user(username='customer2', password='Password123!')
        UserProfile.objects.create(user=self.customer2, phone='3333333333', role='customer')

        self.category = Category.objects.create(name='Chai')
        self.product1 = Product.objects.create(
            name='Cutting Chai', category=self.category, price='15.00', is_available=True
        )
        self.product2 = Product.objects.create(
            name='Bun Maska', category=self.category, price='30.00', is_available=True
        )

    def test_customer_can_create_order_with_calculated_total(self):
        self.client.force_authenticate(user=self.customer1)
        data = {
            'delivery_address': 'Flat 402, Green Avenue',
            'notes': 'Extra hot please',
            'items': [
                {'product_id': self.product1.id, 'quantity': 2},  # 15 * 2 = 30
                {'product_id': self.product2.id, 'quantity': 1},  # 30 * 1 = 30 -> Total 60.00
            ]
        }
        res = self.client.post('/api/v1/orders/', data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        order = Order.objects.get(id=res.data['id'])
        self.assertEqual(order.total_amount, 60.00)
        self.assertEqual(order.items.count(), 2)
        self.assertEqual(order.customer, self.customer1)
        self.assertEqual(order.delivery.status, 'assigned')

    def test_customer_cannot_view_other_customer_orders(self):
        # Create order for customer 1
        order1 = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00)

        # Login customer 2
        self.client.force_authenticate(user=self.customer2)
        res = self.client.get('/api/v1/orders/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 0)

        # Direct access to customer 1's order returns 404
        detail_res = self.client.get(f'/api/v1/orders/{order1.id}/')
        self.assertEqual(detail_res.status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_can_view_and_update_all_orders(self):
        order1 = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00)

        self.client.force_authenticate(user=self.owner)
        res = self.client.get('/api/v1/orders/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)

        # Update order status
        update_res = self.client.patch(f'/api/v1/orders/{order1.id}/', {'status': 'preparing'}, format='json')
        self.assertEqual(update_res.status_code, status.HTTP_200_OK)
        order1.refresh_from_db()
        self.assertEqual(order1.status, 'preparing')
