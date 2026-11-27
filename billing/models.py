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
    Centralized Customer entity for Chai Flow:
    - Name, Mobile number (+91), Delivery address
    - Preserves link to historical Company if migrated
    - Individual billing cycle day (1-31) for monthly billing
    """
    name = models.CharField(max_length=200, db_index=True)
    phone = models.CharField(max_length=15, blank=True, default='', db_index=True)
    address = models.TextField(blank=True, default='')
    billing_cycle_day = models.PositiveSmallIntegerField(
        default=1,
        help_text="Day of month (1-31) when customer monthly billing cycle starts"
    )
    company = models.ForeignKey('Company', on_delete=models.SET_NULL, null=True, blank=True, related_name='individual_contacts')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.phone})"


class CustomerProductPrice(models.Model):
    """
    Customer-specific custom selling price:
    - Overrides the standard product selling price for this specific customer
    - Set and controlled exclusively by Owner
    """
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='custom_prices')
    product = models.ForeignKey('products.Product', on_delete=models.CASCADE, related_name='customer_prices')
    custom_price = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['customer', 'product']
        unique_together = ('customer', 'product')

    def __str__(self):
        return f"{self.customer.name} - {self.product.name} @ ₹{self.custom_price}"


class Invoice(models.Model):
    STATUS_CHOICES = (
        ('unpaid', 'Unpaid'),
        ('partially_paid', 'Partially Paid'),
        ('paid', 'Paid'),
        ('void', 'Void / Cancelled'),
    )
    BILLING_TYPE_CHOICES = (
        ('daily', 'Daily / Single Order'),
        ('quick', 'Quick Billing'),
        ('monthly', 'Monthly Consolidated'),
    )

    invoice_number = models.CharField(max_length=50, unique=True, db_index=True)
    order = models.OneToOneField(
        'orders.Order',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='invoice',
        help_text="One official invoice per order (daily). Blank for monthly consolidated invoices. PROTECT prevents deleting order while invoice exists."
    )
    billing_type = models.CharField(max_length=20, choices=BILLING_TYPE_CHOICES, default='daily', db_index=True)
    billing_period = models.CharField(max_length=7, blank=True, null=True, db_index=True, help_text="YYYY-MM for monthly consolidated invoices")
    company = models.ForeignKey(Company, on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    customer_name = models.CharField(max_length=200, default='Walk-in Customer')
    customer_phone = models.CharField(max_length=15, blank=True, default='')
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    grand_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unpaid')
    notes = models.TextField(blank=True, default='')
    due_date = models.DateField(null=True, blank=True, help_text="Payment due date for this invoice")
    finalized_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when invoice was officially finalized")
    generated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='generated_invoices')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', 'billing_period']),
            models.Index(fields=['billing_type', 'billing_period']),
        ]

    def __str__(self):
        return f"{self.invoice_number} - {self.customer_name} ({self.status})"

    @property
    def total_paid(self):
        # Uses explicit non-reversed PaymentAllocation links, plus any unallocated non-reversed legacy direct payments on this invoice
        from_allocations = self.allocations.filter(is_reversed=False).aggregate(total=models.Sum('allocated_amount'))['total'] or Decimal('0.00')
        from_unallocated_direct = self.payments.filter(allocations__isnull=True, is_reversed=False).aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')
        return from_allocations + from_unallocated_direct

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
    # Phase R1: Historical unit cost and total cost snapshots
    unit_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Historical cost price snapshot at invoice finalization. Null indicates historical cost unavailable."
    )
    total_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Historical total cost snapshot (unit_cost * quantity). Null indicates historical cost unavailable."
    )

    def __str__(self):
        return f"{self.quantity}x {self.product_name} in {self.invoice.invoice_number}"

    @property
    def has_cost_snapshot(self) -> bool:
        return self.unit_cost is not None


class Payment(models.Model):
    """
    Financial Payment Collection:
    - Links to Customer (unified workflow)
    - Can link to a specific Invoice or unallocated Customer account balance
    - Preserves Company reference for backward compatibility
    """
    METHOD_CHOICES = (
        ('cash', 'Cash'),
        ('online', 'Online'),
        ('upi', 'UPI / QR'),
        ('bank_transfer', 'Bank Transfer (NEFT/RTGS)'),
        ('cheque', 'Cheque'),
    )

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, null=True, blank=True, related_name='payments')
    company = models.ForeignKey(Company, on_delete=models.PROTECT, null=True, blank=True, related_name='payments')
    invoice = models.ForeignKey('Invoice', on_delete=models.CASCADE, null=True, blank=True, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default='cash')
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    reference_number = models.CharField(max_length=100, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='recorded_payments')
    is_reversed = models.BooleanField(default=False, db_index=True, help_text="True if payment has been safely reversed.")
    reversal_reason = models.TextField(blank=True, default='', help_text="Mandatory explanation for reversing this payment.")
    reversed_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when payment was reversed.")
    reversed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reversed_payments', help_text="User who authorized reversal.")

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        status_str = " [REVERSED]" if self.is_reversed else ""
        ref = f"for {self.invoice.invoice_number}" if self.invoice else f"for {self.company.name if self.company else 'Account'}"
        return f"Payment of ₹{self.amount} ({self.method}) {ref}{status_str}"

    @property
    def total_allocated(self):
        if self.is_reversed:
            return Decimal('0.00')
        alloc = self.allocations.filter(is_reversed=False).aggregate(s=models.Sum('allocated_amount'))['s']
        return alloc or Decimal('0.00')

    @property
    def unallocated_amount(self):
        if self.is_reversed:
            return Decimal('0.00')
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
    is_reversed = models.BooleanField(default=False, db_index=True, help_text="True if this allocation was reversed.")
    reversed_at = models.DateTimeField(null=True, blank=True)

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


class InvoiceOrder(models.Model):
    """
    Consolidated Billing Linking Model (Phase 1):
    - Links multiple verified orders into one official Invoice.
    - An order can belong to AT MOST ONE official invoice across both legacy and new models.
    - Quick Billing invoices have zero InvoiceOrder records.
    """
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='invoice_orders')
    order = models.OneToOneField(
        'orders.Order',
        on_delete=models.PROTECT,
        related_name='order_billing',
        help_text="One order can belong to only one official invoice."
    )
    consolidated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-consolidated_at']
        indexes = [
            models.Index(fields=['invoice', 'order']),
        ]

    def __str__(self):
        return f"Order #{self.order_id} attached to {self.invoice.invoice_number}"


class DraftInvoice(models.Model):
    """
    Draft Invoice Model (Phase 1 Draft Billing Architecture):
    - Separate from official financial Invoice records.
    - Never mutates official sales, collections, or customer ledgers while in draft status.
    - Editable products, quantities, rates, discounts, custom due dates.
    - Supports multiple billing sources: Quick Billing, Single Order, Consolidated Orders, Recurring.
    """
    BILLING_SOURCE_CHOICES = (
        ('quick_billing', 'Quick Billing'),
        ('single_order', 'Single Order Billing'),
        ('consolidated_orders', 'Consolidated Orders Billing'),
        ('recurring', 'Recurring Order Billing'),
    )

    STATUS_CHOICES = (
        ('draft', 'Draft (Active)'),
        ('finalized', 'Finalized into Official Invoice'),
        ('cancelled', 'Cancelled / Discarded'),
    )

    draft_number = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        help_text="Unique draft reference (e.g. DRAFT-YYYYMMDD-XXXX), strictly distinct from official invoice numbers"
    )
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='draft_invoices')
    customer_name = models.CharField(max_length=200, default='Walk-in Customer')
    customer_phone = models.CharField(max_length=15, blank=True, default='')
    billing_source = models.CharField(max_length=30, choices=BILLING_SOURCE_CHOICES, default='quick_billing')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', db_index=True)

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    grand_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    due_date = models.DateField(null=True, blank=True, help_text="Configurable payment due date for the draft")
    notes = models.TextField(blank=True, default='')

    expires_at = models.DateTimeField(null=True, blank=True, help_text="Optional expiration for temporary order reservations")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_draft_invoices')
    delivery_partner = models.ForeignKey(
        'deliveries.DeliveryPartner',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='draft_invoices',
        help_text="Optional Delivery Partner selected for this draft"
    )
    delivery_address = models.TextField(
        blank=True,
        default='',
        help_text="Specific delivery destination address for this draft (required if delivery partner assigned to walk-in customer)"
    )
    official_invoice = models.OneToOneField(
        Invoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='source_draft',
        help_text="Reference to official invoice once finalized"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.draft_number} - {self.customer_name} [{self.get_billing_source_display()}] ({self.status})"

    @classmethod
    def generate_draft_number(cls):
        date_str = timezone.now().strftime('%Y%m%d')
        today_drafts = cls.objects.filter(draft_number__startswith=f"DRAFT-{date_str}-")
        max_num = 0
        for td in today_drafts:
            try:
                num_part = int(td.draft_number.split('-')[-1])
                if num_part > max_num:
                    max_num = num_part
            except (ValueError, IndexError):
                pass
        return f"DRAFT-{date_str}-{(max_num + 1):04d}"


class DraftInvoiceItem(models.Model):
    """
    Draft Line Item:
    - Independent snapshots; edits do NOT alter original products, OrderItems, or historical InvoiceItems.
    """
    draft = models.ForeignKey(DraftInvoice, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('products.Product', on_delete=models.SET_NULL, null=True, blank=True)
    product_name = models.CharField(max_length=200)
    unit = models.CharField(max_length=50, blank=True, default='Pc')
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    # Phase R1: Cost snapshot in draft
    unit_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Product cost price snapshot at draft creation/update time."
    )
    total_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total cost snapshot (unit_cost * quantity)."
    )

    def __str__(self):
        return f"{self.quantity}x {self.product_name} in {self.draft.draft_number}"


class DraftInvoiceOrder(models.Model):
    """
    Order Reservation in Draft Billing:
    - Links one draft to one or more verified orders of the same customer.
    - An order cannot be attached to multiple active drafts.
    - Cancelling a draft releases its reserved orders.
    """
    draft = models.ForeignKey(DraftInvoice, on_delete=models.CASCADE, related_name='draft_orders')
    order = models.ForeignKey(
        'orders.Order',
        on_delete=models.CASCADE,
        related_name='draft_reservations',
        help_text="Order reserved in this draft invoice"
    )
    reserved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-reserved_at']
        indexes = [
            models.Index(fields=['draft', 'order']),
        ]

    def __str__(self):
        return f"Order #{self.order_id} reserved in {self.draft.draft_number}"


class Expense(models.Model):
    """
    Phase R1: Business Operating Expense Entity.
    - Owner-only access.
    - Tracks business operational costs (Rent, Electricity, Utilities, Transport, etc.).
    - Strictly distinct from raw material inventory / COGS.
    """
    CATEGORY_CHOICES = (
        ('rent', 'Rent'),
        ('electricity_utilities', 'Electricity & Utilities'),
        ('transport', 'Transport & Fuel'),
        ('packaging', 'Packaging Materials'),
        ('maintenance', 'Maintenance & Repairs'),
        ('administrative', 'Administrative & Office'),
        ('salaries', 'Staff & Partner Wages'),
        ('marketing', 'Marketing & Promotion'),
        ('other_operating', 'Other Operating Expenses'),
    )

    PAYMENT_METHOD_CHOICES = (
        ('cash', 'Cash'),
        ('online', 'Online'),
        ('upi', 'UPI / QR'),
        ('bank_transfer', 'Bank Transfer (NEFT/RTGS)'),
        ('cheque', 'Cheque'),
    )

    title = models.CharField(max_length=200, db_index=True)
    category = models.CharField(max_length=40, choices=CATEGORY_CHOICES, default='other_operating', db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    expense_date = models.DateField(default=timezone.now, db_index=True)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default='cash')
    notes = models.TextField(blank=True, default='')
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='recorded_expenses')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-expense_date', '-created_at']
        indexes = [
            models.Index(fields=['expense_date', 'category']),
        ]

    def __str__(self):
        return f"{self.title} (₹{self.amount}) [{self.get_category_display()}] on {self.expense_date}"


class InvoiceAuditLog(models.Model):
    """
    Part 4: Audit trail for Owner edits to finalized/saved bills.
    For every edit, captures:
    - Bill ID
    - Previous values (items snapshot, previous subtotal, discount, grand total)
    - Updated values (items snapshot, updated subtotal, discount, grand total)
    - Edited by User
    - Edit timestamp
    - Previous and updated grand totals
    - Edit notes / reason
    """
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='audit_logs')
    edited_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='invoice_audits')
    previous_total = models.DecimalField(max_digits=10, decimal_places=2)
    updated_total = models.DecimalField(max_digits=10, decimal_places=2)
    previous_values = models.JSONField(help_text="Snapshot of previous items, subtotal, discount, and notes")
    updated_values = models.JSONField(help_text="Snapshot of updated items, subtotal, discount, and notes")
    reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Audit for Invoice #{self.invoice_id} at {self.created_at} by {self.edited_by.username} (₹{self.previous_total} -> ₹{self.updated_total})"


