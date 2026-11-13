import os
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from billing.models import Customer, Invoice, InvoiceItem, Payment
from products.models import Product, Category

User = get_user_model()

class InvoicePDFTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='9876543210',
            password='testpassword',
            is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

        self.category = Category.objects.create(name="Chai & Snacks")
        self.product1 = Product.objects.create(
            name="Special Masala Chai",
            category=self.category,
            price=Decimal("20.00"),
            is_available=True
        )
        self.product2 = Product.objects.create(
            name="Butter Bun Maska",
            category=self.category,
            price=Decimal("40.00"),
            is_available=True
        )

    def test_valid_invoice_pdf_generation(self):
        # 1. Create a customer invoice
        customer = Customer.objects.create(
            name="Vikram Singh",
            phone="9876500001",
            address="Shop 12, Main Market"
        )
        invoice = Invoice.objects.create(
            invoice_number="INV-20261112-0001",
            customer=customer,
            customer_name=customer.name,
            customer_phone=customer.phone,
            subtotal=Decimal("80.00"),
            grand_total=Decimal("80.00"),
            notes="Please deliver hot"
        )
        InvoiceItem.objects.create(
            invoice=invoice,
            product=self.product1,
            product_name=self.product1.name,
            unit_price=Decimal("20.00"),
            quantity=2,
            total_price=Decimal("40.00")
        )
        InvoiceItem.objects.create(
            invoice=invoice,
            product=self.product2,
            product_name=self.product2.name,
            unit_price=Decimal("40.00"),
            quantity=1,
            total_price=Decimal("40.00")
        )
        Payment.objects.create(
            invoice=invoice,
            amount=Decimal("50.00"),
            method="cash"
        )
        invoice.update_payment_status()

        # Call PDF endpoint
        response = self.client.get(f"/api/v1/billing/invoices/{invoice.id}/pdf/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response['Content-Disposition'].startswith('inline; filename='))
        self.assertTrue(len(response.content) > 1000)
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_walkin_customer_invoice_pdf(self):
        # Walk-in customer (no Customer FK)
        invoice = Invoice.objects.create(
            invoice_number="INV-20261112-0002",
            customer=None,
            customer_name="Walk-in Customer",
            subtotal=Decimal("40.00"),
            grand_total=Decimal("40.00")
        )
        InvoiceItem.objects.create(
            invoice=invoice,
            product=self.product1,
            product_name=self.product1.name,
            unit_price=Decimal("20.00"),
            quantity=2,
            total_price=Decimal("40.00")
        )
        Payment.objects.create(
            invoice=invoice,
            amount=Decimal("40.00"),
            method="cash"
        )
        invoice.update_payment_status()

        response = self.client.get(f"/api/v1/billing/invoices/{invoice.id}/pdf/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_unauthorized_pdf_request(self):
        unauth_client = APIClient()
        response = unauth_client.get("/api/v1/billing/invoices/1/pdf/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_invalid_invoice_id_returns_404(self):
        response = self.client.get("/api/v1/billing/invoices/999999/pdf/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_historical_snapshot_preserved_in_pdf(self):
        # Invoice created with unit price 20
        invoice = Invoice.objects.create(
            invoice_number="INV-20261112-0003",
            subtotal=Decimal("20.00"),
            grand_total=Decimal("20.00")
        )
        item = InvoiceItem.objects.create(
            invoice=invoice,
            product=self.product1,
            product_name=self.product1.name,
            unit_price=Decimal("20.00"),
            quantity=1,
            total_price=Decimal("20.00")
        )
        # Update product price in master database
        self.product1.price = Decimal("50.00")
        self.product1.save()

        # PDF must still generate with item snapshot
        response = self.client.get(f"/api/v1/billing/invoices/{invoice.id}/pdf/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify invoice record did not get mutated
        item.refresh_from_db()
        self.assertEqual(item.unit_price, Decimal("20.00"))

    def test_multipage_invoice_pdf(self):
        invoice = Invoice.objects.create(
            invoice_number="INV-20261112-0004",
            subtotal=Decimal("1000.00"),
            grand_total=Decimal("1000.00")
        )
        # Add 35 items to force multiple pages in A4
        for i in range(1, 36):
            InvoiceItem.objects.create(
                invoice=invoice,
                product=None,
                product_name=f"Bulk Inventory Batch Item #{i}",
                unit_price=Decimal("25.00"),
                quantity=1,
                total_price=Decimal("25.00")
            )

        response = self.client.get(f"/api/v1/billing/invoices/{invoice.id}/pdf/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))

