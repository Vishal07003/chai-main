from django.db import models
from decimal import Decimal
from django.utils import timezone
import uuid

class Customer(models.Model):
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=15, db_index=True)
    address = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.phone})"

class Invoice(models.Model):
    STATUS_CHOICES = (
        ('unpaid', 'Unpaid'),
        ('partially_paid', 'Partially Paid'),
        ('paid', 'Paid'),
    )

    invoice_number = models.CharField(max_length=50, unique=True, db_index=True)
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    customer_name = models.CharField(max_length=200, default='Walk-in Customer')
    customer_phone = models.CharField(max_length=15, blank=True, default='')
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    grand_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unpaid')
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.invoice_number} - {self.customer_name} ({self.status})"

    @property
    def total_paid(self):
        total = self.payments.aggregate(total=models.Sum('amount'))['total']
        return total or Decimal('0.00')

    @property
    def pending_amount(self):
        return max(Decimal('0.00'), self.grand_total - self.total_paid)

    def update_payment_status(self):
        paid = self.total_paid
        if paid >= self.grand_total and self.grand_total > 0:
            self.status = 'paid'
        elif paid > 0:
            self.status = 'partially_paid'
        else:
            self.status = 'unpaid'
        self.save(update_fields=['status'])

class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('products.Product', on_delete=models.SET_NULL, null=True, blank=True)
    product_name = models.CharField(max_length=200)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.quantity}x {self.product_name} in {self.invoice.invoice_number}"

class Payment(models.Model):
    METHOD_CHOICES = (
        ('cash', 'Cash'),
        ('online', 'Online'),
    )

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default='cash')
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        return f"Payment of ₹{self.amount} ({self.method}) for {self.invoice.invoice_number}"
