from datetime import date
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from accounts.models import UserProfile
from billing.models import Customer, Invoice, InvoiceItem, InvoiceAuditLog
from products.models import Product
from deliveries.models import DeliveryPartner
from orders.models import Order, OrderItem
from billing.invoicing_service import InvoicingService


class DailyBillingWorkflowTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()

        # Owner user & profile
        self.owner = User.objects.create_user(
            username="owner_user",
            email="owner@chaitest.com",
            password="testpassword123",
            first_name="Owner",
            last_name="User"
        )
        self.owner_profile = UserProfile.objects.create(
            user=self.owner,
            phone="9876543210",
            role="owner"
        )
        self.client.force_authenticate(user=self.owner)

        # Customer User & Profile
        self.cust_user = User.objects.create_user(
            username="ramesh_tea",
            password="testpassword123",
            first_name="Ramesh",
            last_name="Kumar"
        )
        self.customer = Customer.objects.create(
            name="Ramesh Tea Stall",
            phone="9988776655",
            address="Connaught Place, Delhi"
        )

        # Products
        self.chai_powder = Product.objects.create(
            name="Special CTC Chai Powder (1kg)",
            price=Decimal("450.00"),
            is_available=True
        )
        self.sugar = Product.objects.create(
            name="Refined Sugar (5kg)",
            price=Decimal("250.00"),
            is_available=True
        )

    def test_unbilled_orders_filtering(self):
        """Test filtering orders with ?unbilled=true returns only unbilled delivered orders."""
        # 1. Delivered order without invoice
        order_delivered_unbilled = Order.objects.create(
            customer=self.cust_user,
            customer_profile=self.customer,
            status="delivered",
            total_amount=Decimal("900.00"),
            delivery_address=self.customer.address
        )
        OrderItem.objects.create(
            order=order_delivered_unbilled,
            product=self.chai_powder,
            quantity=2,
            price=Decimal("450.00")
        )

        # 2. Delivered order with invoice
        order_delivered_billed = Order.objects.create(
            customer=self.cust_user,
            customer_profile=self.customer,
            status="delivered",
            total_amount=Decimal("450.00"),
            delivery_address=self.customer.address
        )
        OrderItem.objects.create(
            order=order_delivered_billed,
            product=self.chai_powder,
            quantity=1,
            price=Decimal("450.00")
        )
        InvoicingService.generate_manual_invoice(
            order=order_delivered_billed,
            confirmed_items=[{"product_id": self.chai_powder.id, "quantity": 1}],
            owner_user=self.owner
        )

        # 3. Pending order (not delivered)
        order_pending = Order.objects.create(
            customer=self.cust_user,
            customer_profile=self.customer,
            status="pending",
            total_amount=Decimal("250.00"),
            delivery_address=self.customer.address
        )

        response = self.client.get("/api/v1/orders/?status=delivered&unbilled=true")
        self.assertEqual(response.status_code, 200)

        results = response.data.get("results", response.data) if isinstance(response.data, dict) else response.data
        order_ids = [o["id"] for o in results]

        self.assertIn(order_delivered_unbilled.id, order_ids)
        self.assertNotIn(order_delivered_billed.id, order_ids)
        self.assertNotIn(order_pending.id, order_ids)

    def test_generate_invoice_with_rate_reviews_and_discounts(self):
        """Owner reviews delivered items, adjusts rates & discount, then generates manual bill."""
        order = Order.objects.create(
            customer=self.cust_user,
            customer_profile=self.customer,
            status="delivered",
            total_amount=Decimal("1150.00"),
            delivery_address=self.customer.address
        )
        # Owner reviews rate for CTC powder down to 400, adds a 50 rupee discount
        payload = {
            "items": [
                {"product_id": self.chai_powder.id, "quantity": 2, "unit_price": "400.00"},
                {"product_id": self.sugar.id, "quantity": 1, "unit_price": "250.00"}
            ],
            "discount_amount": "50.00",
            "notes": "Reviewed delivered items with discount applied."
        }

        response = self.client.post(f"/api/v1/orders/{order.id}/generate-invoice/", payload, format="json")
        self.assertEqual(response.status_code, 201)

        # Subtotal: (2 * 400) + (1 * 250) = 1050. Grand Total: 1050 - 50 = 1000.
        invoice_data = response.data
        self.assertEqual(Decimal(str(invoice_data["subtotal"])), Decimal("1050.00"))
        self.assertEqual(Decimal(str(invoice_data["discount_amount"])), Decimal("50.00"))
        self.assertEqual(Decimal(str(invoice_data["grand_total"])), Decimal("1000.00"))

        # Verify duplicate generation prevention returns 409
        dup_response = self.client.post(f"/api/v1/orders/{order.id}/generate-invoice/", payload, format="json")
        self.assertEqual(dup_response.status_code, 409)

    def test_edit_saved_bill_creates_audit_log_and_updates_ledger(self):
        """Owner edits an existing bill, creates immutable audit log, and keeps amounts consistent."""
        order = Order.objects.create(
            customer=self.cust_user,
            customer_profile=self.customer,
            status="delivered",
            total_amount=Decimal("900.00"),
            delivery_address=self.customer.address
        )
        OrderItem.objects.create(
            order=order,
            product=self.chai_powder,
            quantity=2,
            price=Decimal("450.00")
        )

        # Generate initial invoice
        invoice = InvoicingService.generate_manual_invoice(
            order=order,
            confirmed_items=[{"product_id": self.chai_powder.id, "quantity": 2, "unit_price": "450.00"}],
            owner_user=self.owner
        )
        self.assertEqual(invoice.grand_total, Decimal("900.00"))

        # Edit bill: Change quantity to 3, add discount of 100
        edit_payload = {
            "items": [
                {
                    "product_id": self.chai_powder.id,
                    "product_name": self.chai_powder.name,
                    "quantity": 3,
                    "unit_price": "450.00"
                }
            ],
            "discount_amount": "100.00",
            "reason": "Customer requested 1 additional packet upon driver arrival.",
            "notes": "Updated bill after customer phone call."
        }

        response = self.client.patch(f"/api/v1/billing/invoices/{invoice.id}/edit_bill/", edit_payload, format="json")
        self.assertEqual(response.status_code, 200)

        # Updated subtotal: 3 * 450 = 1350. Grand Total: 1350 - 100 = 1250.
        self.assertEqual(Decimal(str(response.data["subtotal"])), Decimal("1350.00"))
        self.assertEqual(Decimal(str(response.data["discount_amount"])), Decimal("100.00"))
        self.assertEqual(Decimal(str(response.data["grand_total"])), Decimal("1250.00"))

        # Verify audit log was created
        audit_logs = InvoiceAuditLog.objects.filter(invoice=invoice)
        self.assertEqual(audit_logs.count(), 1)
        log = audit_logs.first()
        self.assertEqual(log.previous_total, Decimal("900.00"))
        self.assertEqual(log.updated_total, Decimal("1250.00"))
        self.assertEqual(log.reason, "Customer requested 1 additional packet upon driver arrival.")
        self.assertEqual(log.edited_by, self.owner)
