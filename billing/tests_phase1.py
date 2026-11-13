from django.test import TestCase
from django.contrib.auth.models import User
from decimal import Decimal
from django.utils import timezone
from products.models import Product, Category
from orders.models import Order, OrderItem, PriceOverrideRequest
from billing.models import Company, CompanyProductPrice, Invoice, InvoiceItem, Payment, PaymentAllocation, LedgerAdjustment
from billing.invoicing_service import InvoicingService
from billing.ledger_service import LedgerService
from billing.payment_service import PaymentAllocationService
from deliveries.models import Delivery, DeliveryItem
from rest_framework.exceptions import ValidationError, PermissionDenied

class Phase1ArchitectureTests(TestCase):
    def setUp(self):
        # 1. Users
        self.owner_user = User.objects.create_user(username='owner1', password='pw', is_staff=True)
        self.staff_user = User.objects.create_user(username='staff1', password='pw')
        from accounts.models import UserProfile
        UserProfile.objects.create(user=self.owner_user, phone='9999900001', role='owner')
        UserProfile.objects.create(user=self.staff_user, phone='9999900002', role='customer')

        # 2. Company & Products
        self.company = Company.objects.create(
            name='Acme Corp',
            trade_name='Acme Tech',
            phone='9876543210',
            billing_cycle='BOTH'
        )
        self.category = Category.objects.create(name='Beverages')
        self.chai = Product.objects.create(name='Masala Chai Flask', price=Decimal('100.00'), category=self.category)
        self.samosa = Product.objects.create(name='Mini Samosa Plate', price=Decimal('50.00'), category=self.category)

        # 3. Contracted Slab: Acme gets Chai at Rs.85 if ordering >= 20 units
        CompanyProductPrice.objects.create(
            company=self.company,
            product=self.chai,
            custom_price=Decimal('85.00'),
            min_quantity=20,
            valid_from=timezone.now().date()
        )

    def test_tc1_create_order_without_generating_invoice(self):
        """TC-1: Order creation never automatically creates an invoice"""
        order = Order.objects.create(
            customer=self.staff_user,
            company=self.company,
            delivery_address='Acme Tower 4',
            total_amount=Decimal('1000.00')
        )
        OrderItem.objects.create(order=order, product=self.chai, quantity=10, price=Decimal('100.00'))

        self.assertFalse(order.has_invoice)
        self.assertEqual(Invoice.objects.filter(order=order).count(), 0)

    def test_tc2_complete_delivery_without_generating_invoice(self):
        """TC-2: Completing delivery never automatically generates an invoice"""
        order = Order.objects.create(
            customer=self.staff_user,
            company=self.company,
            delivery_address='Acme Tower 4'
        )
        OrderItem.objects.create(order=order, product=self.chai, quantity=10, price=Decimal('100.00'))

        delivery = Delivery.objects.create(order=order, status='delivered')
        DeliveryItem.objects.create(delivery=delivery, product=self.chai, ordered_quantity=10, delivered_quantity=10)

        order.refresh_from_db()
        self.assertFalse(order.has_invoice)
        self.assertEqual(Invoice.objects.filter(order=order).count(), 0)

    def test_tc3_owner_manually_generates_invoice(self):
        """TC-3: Owner manually triggers invoice generation with backend price validation"""
        order = Order.objects.create(
            customer=self.staff_user,
            company=self.company,
            delivery_address='Acme Tower 4'
        )
        OrderItem.objects.create(order=order, product=self.chai, quantity=25, price=Decimal('100.00'))

        # Owner generates invoice for confirmed 25 units -> resolves to slab rate Rs.85
        invoice = InvoicingService.generate_manual_invoice(
            order=order,
            confirmed_items=[{'product_id': self.chai.id, 'quantity': 25}],
            owner_user=self.owner_user
        )

        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.order, order)
        self.assertEqual(invoice.grand_total, Decimal('2125.00')) # 25 * 85
        self.assertEqual(invoice.status, 'unpaid')

    def test_tc4_prevent_duplicate_invoice_generation(self):
        """TC-4: Prevent duplicate invoice generation for the same order"""
        order = Order.objects.create(customer=self.staff_user, company=self.company, delivery_address='Tower')
        OrderItem.objects.create(order=order, product=self.chai, quantity=5, price=Decimal('100.00'))

        InvoicingService.generate_manual_invoice(
            order=order,
            confirmed_items=[{'product_id': self.chai.id, 'quantity': 5}],
            owner_user=self.owner_user
        )

        with self.assertRaises(ValidationError):
            InvoicingService.generate_manual_invoice(
                order=order,
                confirmed_items=[{'product_id': self.chai.id, 'quantity': 5}],
                owner_user=self.owner_user
            )

    def test_tc5_delivered_quantity_lower_than_ordered(self):
        """TC-5: Owner confirms actual delivered quantity lower than ordered quantity"""
        order = Order.objects.create(customer=self.staff_user, company=self.company, delivery_address='Tower')
        OrderItem.objects.create(order=order, product=self.chai, quantity=10, price=Decimal('100.00'))

        # Delivery partner delivered 8 (2 damaged)
        delivery = Delivery.objects.create(order=order, status='delivered')
        d_item = DeliveryItem.objects.create(delivery=delivery, product=self.chai, ordered_quantity=10, delivered_quantity=8)
        self.assertTrue(d_item.has_discrepancy)

        # Owner confirms 8 units for invoicing
        invoice = InvoicingService.generate_manual_invoice(
            order=order,
            confirmed_items=[{'product_id': self.chai.id, 'quantity': 8}],
            owner_user=self.owner_user
        )

        self.assertEqual(invoice.grand_total, Decimal('800.00')) # 8 * 100
        inv_item = invoice.items.first()
        self.assertEqual(inv_item.quantity, 8)

    def test_tc6_fifo_payment_allocation_and_advance_credit(self):
        """TC-6: FIFO payment allocation across multiple invoices & unallocated advance"""
        inv1 = Invoice.objects.create(
            invoice_number='INV-TEST-001', company=self.company, grand_total=Decimal('600.00'), status='unpaid'
        )
        inv2 = Invoice.objects.create(
            invoice_number='INV-TEST-002', company=self.company, grand_total=Decimal('500.00'), status='unpaid'
        )

        # Payment of Rs. 1500 collected
        pm = Payment.objects.create(
            company=self.company,
            amount=Decimal('1500.00'),
            method='bank_transfer',
            reference_number='UTR12345'
        )

        alloc_res = PaymentAllocationService.allocate_payment_fifo(payment=pm, company=self.company)

        inv1.refresh_from_db()
        inv2.refresh_from_db()

        self.assertEqual(inv1.status, 'paid')
        self.assertEqual(inv1.total_paid, Decimal('600.00'))

        self.assertEqual(inv2.status, 'paid')
        self.assertEqual(inv2.total_paid, Decimal('500.00'))

        # 1500 - (600 + 500) = 400 advance credit
        self.assertEqual(Decimal(alloc_res['unallocated_advance']), Decimal('400.00'))
        self.assertEqual(pm.unallocated_amount, Decimal('400.00'))

    def test_tc7_dynamic_monthly_statement_calculation(self):
        """TC-7: Dynamic Monthly Statement balances (Opening + Invoices + Adjustments - Payments = Closing)"""
        now = timezone.now()
        year = now.year
        month = now.month

        # Invoices
        Invoice.objects.create(
            invoice_number='INV-STMT-01', company=self.company, grand_total=Decimal('2000.00'), status='unpaid'
        )
        Invoice.objects.create(
            invoice_number='INV-STMT-02', company=self.company, grand_total=Decimal('1000.00'), status='unpaid'
        )

        # Payment
        Payment.objects.create(
            company=self.company, amount=Decimal('1500.00'), method='online'
        )

        # Authorized Adjustment: Credit Rs. 200 for spilled cups
        LedgerAdjustment.objects.create(
            company=self.company,
            adjustment_type='CREDIT',
            amount=Decimal('200.00'),
            reason='Damage compensation',
            approved_by=self.owner_user
        )

        stmt = LedgerService.get_monthly_statement(company=self.company, year=year, month=month)

        self.assertEqual(stmt['summary']['opening_balance'], '0.00')
        self.assertEqual(stmt['summary']['total_debit'], '3000.00') # 2000 + 1000
        self.assertEqual(stmt['summary']['total_credit'], '1700.00') # 1500 payment + 200 credit adjustment
        self.assertEqual(stmt['summary']['closing_balance'], '1300.00') # 3000 - 1700

    def test_tc8_non_owner_blocked_from_generating_invoice(self):
        """TC-8: Non-owner user blocked from generating invoices"""
        order = Order.objects.create(customer=self.staff_user, company=self.company, delivery_address='Tower')
        OrderItem.objects.create(order=order, product=self.chai, quantity=5, price=Decimal('100.00'))

        with self.assertRaises(PermissionDenied):
            InvoicingService.generate_manual_invoice(
                order=order,
                confirmed_items=[{'product_id': self.chai.id, 'quantity': 5}],
                owner_user=self.staff_user
            )

