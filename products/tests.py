from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from accounts.models import UserProfile
from products.models import Category, Product

class ProductsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(username='chaiowner', password='Password123!')
        UserProfile.objects.create(user=self.owner, phone='1111111111', role='owner')

        self.customer = User.objects.create_user(username='customer1', password='Password123!')
        UserProfile.objects.create(user=self.customer, phone='2222222222', role='customer')

        self.category = Category.objects.create(name='Hot Tea', description='Warm masala chai & more')
        self.product = Product.objects.create(
            name='Ginger Chai',
            category=self.category,
            price='20.00',
            description='Fresh ginger chai',
            is_available=True
        )

    def test_public_can_view_products(self):
        res = self.client.get('/api/v1/products/items/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]['name'], 'Ginger Chai')

    def test_customer_cannot_create_product(self):
        self.client.force_authenticate(user=self.customer)
        res = self.client.post('/api/v1/products/items/', {
            'name': 'Unauthorized Product',
            'price': '50.00',
            'category': self.category.id
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_create_product(self):
        self.client.force_authenticate(user=self.owner)
        res = self.client.post('/api/v1/products/items/', {
            'name': 'Masala Chai',
            'price': '25.00',
            'category': self.category.id,
            'is_available': True
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Product.objects.count(), 2)
