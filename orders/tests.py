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
        order1 = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00)

        self.client.force_authenticate(user=self.customer2)
        res = self.client.get('/api/v1/orders/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 0)

        detail_res = self.client.get(f'/api/v1/orders/{order1.id}/')
        self.assertEqual(detail_res.status_code, status.HTTP_404_NOT_FOUND)

    def test_customer_can_cancel_own_pending_order(self):
        """Customers are permitted to cancel their own order only while status is pending."""
        order = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00, status='pending')
        self.client.force_authenticate(user=self.customer1)

        res = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'cancelled'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')

    def test_customer_cannot_cancel_already_accepted_order(self):
        """Security: customers cannot cancel once the order has been accepted/preparing."""
        order = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00, status='accepted')
        self.client.force_authenticate(user=self.customer1)

        res = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'cancelled'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        self.assertEqual(order.status, 'accepted')

    def test_customer_cannot_force_order_to_delivered(self):
        """Security: customers cannot advance order statuses (e.g. to delivered)."""
        order = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00, status='pending')
        self.client.force_authenticate(user=self.customer1)

        res = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'delivered'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending')

    def test_customer_cannot_reassign_order_customer(self):
        """Security: customer cannot reassign the owner of the order to someone else."""
        order = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00, status='pending')
        self.client.force_authenticate(user=self.customer1)

        res = self.client.patch(f'/api/v1/orders/{order.id}/', {'customer': self.customer2.id}, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_status_transitions_workflow(self):
        """Owner must follow valid state machine transitions: pending -> accepted -> preparing -> out_for_delivery -> delivered."""
        order = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00, status='pending')
        self.client.force_authenticate(user=self.owner)

        # 1. pending -> accepted: Valid
        res = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'accepted'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # 2. accepted -> delivered: Invalid jump (must be preparing or cancelled)
        res_invalid = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'delivered'}, format='json')
        self.assertEqual(res_invalid.status_code, status.HTTP_400_BAD_REQUEST)

        # 3. accepted -> preparing: Valid
        res2 = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'preparing'}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)

        # 4. preparing -> out_for_delivery: Valid
        res3 = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'out_for_delivery'}, format='json')
        self.assertEqual(res3.status_code, status.HTTP_200_OK)

        # 5. out_for_delivery -> delivered: Valid
        res4 = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'delivered'}, format='json')
        self.assertEqual(res4.status_code, status.HTTP_200_OK)
