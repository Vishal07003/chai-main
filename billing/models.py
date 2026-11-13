from django.db import models
from django.contrib.auth.models import User
from decimal import Decimal
from django.utils import timezone
import uuid

class Company(models.Model):
    """
    B2B Company Account entity:
    - Dedicated credit limits & payment terms
    - Dynamic ledger tracking (opening, invoices, payments, adjustments, closing)
    """
    BILLING_CYCLE_CHOICES = (
        ('DAILY', 'Daily Invoicing'),
        ('WEEKLY', 'Weekly Statement'),
        ('MONTHLY', 'Monthly Statement'),
        ('BOTH', 'Daily + Monthly Statement'),
    )

    name = models.CharField(max_length=200, db_index=True)
    trade_name = models.CharField(max_length=200, blank=True, default='')
    phone = models.CharField(max_length=15, db_index=True)
    email = models.EmailField(blank=True, default='')
    gstin = models.CharField(max_length=20, blank=True, default='', db_index=True)
    billing_address = models.TextField(blank=True, default='')
    billing_cycle = models.CharField(max_length=20, choices=BILLING_CYCLE_CHOICES, default='BOTH')
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    payment_terms_days = models.PositiveIntegerField(default=30)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Companies'

    def __str__(self):
        return f"{self.name} ({self.phone})"


class CompanyProductPrice(models.Model):
    """
    Contracted / Agreed pricing per company:
    - Supports volume quantity slabs (min_quantity)
    - Effective date range (valid_from, valid_to)
    """
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='contracted_prices')
    product = models.ForeignKey('products.Product', on_delete=models.CASCADE, related_name='company_prices')
    custom_price = models.DecimalField(max_digits=10, decimal_places=2)
    min_quantity = models.PositiveIntegerField(default=1, help_text="Minimum quantity for this price slab")
    valid_from = models.DateField(default=timezone.now)
    valid_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['company', 'product', '-min_quantity']
        indexes = [
            models.Index(fields=['company', 'product', 'is_active']),
        ]

    def __str__(self):
        return f"{self.company.name} - {self.product.name} @ ₹{self.custom_price} (min {self.min_quantity})"


class Customer(models.Model):
    """
    Walk-in / retail credit customer model (preserved for Quick Billing)
    """
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=15, db_index=True)
    address = models.TextField(blank=True, default='')
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True, related_name='individual_contacts')
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
        ('void', 'Void / Cancelled'),
    )

    invoice_number = models.CharField(max_length=50, unique=True, db_index=True)
    order = models.OneToOneField(
        'orders.Order',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='invoice',
        help_text="One official invoice per order. PROTECT prevents deleting order while invoice exists."
    )
    company = models.ForeignKey(Company, on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    customer_name = models.CharField(max_length=200, default='Walk-in Customer')
    customer_phone = models.CharField(max_length=15, blank=True, default='')
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    grand_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unpaid')
    notes = models.TextField(blank=True, default='')
    generated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='generated_invoices')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.invoice_number} - {self.customer_name} ({self.status})"

    @property
    def total_paid(self):
        # Uses direct Payment links (legacy) or PaymentAllocation links
        from_allocations = self.allocations.aggregate(total=models.Sum('allocated_amount'))['total']
        if from_allocations is not None:
            return from_allocations
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
    """
    Financial Payment Collection:
    - Can be linked to a single Invoice (legacy / walk-in)
    - Can be linked to a Company (B2B bulk payment or advance)
    """
    METHOD_CHOICES = (
        ('cash', 'Cash'),
        ('online', 'Online'),
        ('upi', 'UPI / QR'),
        ('bank_transfer', 'Bank Transfer (NEFT/RTGS)'),
        ('cheque', 'Cheque'),
    )

    company = models.ForeignKey(Company, on_delete=models.PROTECT, null=True, blank=True, related_name='payments')
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, null=True, blank=True, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default='cash')
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    reference_number = models.CharField(max_length=100, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='recorded_payments')

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        ref = f"for {self.invoice.invoice_number}" if self.invoice else f"for {self.company.name if self.company else 'Account'}"
        return f"Payment of ₹{self.amount} ({self.method}) {ref}"

    @property
    def total_allocated(self):
        alloc = self.allocations.aggregate(s=models.Sum('allocated_amount'))['s']
        return alloc or Decimal('0.00')

    @property
    def unallocated_amount(self):
        return max(Decimal('0.00'), self.amount - self.total_allocated)


class PaymentAllocation(models.Model):
    """
    Explicit M:N Payment-to-Invoice Allocation:
    - Supports allocating one payment across multiple invoices (FIFO)
    - Supports partial invoice payments
    - Full audit history of when, how much, and against which invoice funds were applied
    """
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='allocations')
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='allocations')
    allocated_amount = models.DecimalField(max_digits=10, decimal_places=2)
    allocated_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-allocated_at']
        indexes = [
            models.Index(fields=['payment', 'invoice']),
        ]

    def __str__(self):
        return f"Allocated ₹{self.allocated_amount} from Payment #{self.payment.id} to {self.invoice.invoice_number}"


class LedgerAdjustment(models.Model):
    """
    Authorized Credit/Debit memo adjustment for company passbook reconciliation:
    - CREDIT: reduces outstanding company balance (e.g. damaged flasks, return of unsold items)
    - DEBIT: increases company balance (e.g. penalty, late fee, extra supplies)
    - Must be authorized by Owner
    """
    ADJUSTMENT_TYPE_CHOICES = (
        ('CREDIT', 'Credit Memo (Reduces Balance)'),
        ('DEBIT', 'Debit Memo (Increases Balance)'),
    )

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='adjustments')
    adjustment_type = models.CharField(max_length=10, choices=ADJUSTMENT_TYPE_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.TextField()
    approved_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='approved_adjustments')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.adjustment_type} ₹{self.amount} for {self.company.name} ({self.reason[:30]})"
