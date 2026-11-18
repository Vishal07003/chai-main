from django.db import models
from django.contrib.auth.models import User
from decimal import Decimal
from products.models import Product

class Order(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('preparing', 'Preparing'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    )

    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='orders')
    customer_profile = models.ForeignKey('billing.Customer', on_delete=models.SET_NULL, null=True, blank=True, related_name='customer_orders')
    company = models.ForeignKey('billing.Company', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    delivery_address = models.TextField()
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        comp = f" [{self.company.name}]" if self.company else ""
        return f"Order #{self.id} - {self.customer.username}{comp} ({self.status})"

    @property
    def has_invoice(self) -> bool:
        return hasattr(self, 'invoice') and self.invoice is not None


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.quantity}x {self.product.name if self.product else 'Item'}"


class PriceOverrideRequest(models.Model):
    """
    Owner-Only Discount and Price Override Approval:
    - Recorded when an order item requests a price below the standard/contracted rate
    - Only user.profile.role == 'owner' can approve or reject
    """
    STATUS_CHOICES = (
        ('PENDING', 'Pending Owner Review'),
        ('APPROVED', 'Approved by Owner'),
        ('REJECTED', 'Rejected by Owner'),
    )

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='price_override_requests')
    order_item = models.ForeignKey(OrderItem, on_delete=models.CASCADE, related_name='override_requests')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    standard_price = models.DecimalField(max_digits=10, decimal_places=2)
    requested_price = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    quantity = models.PositiveIntegerField(default=1)
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING', db_index=True)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='requested_price_overrides')
    reviewed_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name='reviewed_price_overrides')
    decision_timestamp = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Override for Order #{self.order.id} ({self.product.name}): ₹{self.standard_price} -> ₹{self.requested_price} [{self.status}]"


class RecurringOrder(models.Model):
    """
    Template for recurring orders for regular customers:
    - Frequency: DAILY or specific weekdays (comma-separated: 0=Mon, 1=Tue, ..., 6=Sun)
    - Active / Paused state
    - Delivery address & optional default delivery partner
    - Supports skipping specific dates (stored in skipped_dates JSON list: ["YYYY-MM-DD", ...])
    """
    FREQUENCY_CHOICES = (
        ('daily', 'Daily'),
        ('weekdays', 'Specific Weekdays'),
    )

    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='recurring_orders')
    customer_profile = models.ForeignKey('billing.Customer', on_delete=models.SET_NULL, null=True, blank=True, related_name='recurring_orders')
    company = models.ForeignKey('billing.Company', on_delete=models.SET_NULL, null=True, blank=True, related_name='recurring_orders')
    delivery_partner = models.ForeignKey('deliveries.DeliveryPartner', on_delete=models.SET_NULL, null=True, blank=True, related_name='recurring_orders')
    delivery_address = models.TextField()
    frequency = models.CharField(max_length=20, choices=FREQUENCY_CHOICES, default='daily')
    weekdays = models.CharField(max_length=50, blank=True, default='', help_text="Comma-separated weekday numbers (0=Mon, 1=Tue, ..., 6=Sun)")
    is_paused = models.BooleanField(default=False, db_index=True)
    skipped_dates = models.JSONField(default=list, blank=True, help_text="List of ISO date strings YYYY-MM-DD to skip")
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        name = self.customer_profile.name if self.customer_profile else self.customer.username
        return f"RecurringOrder #{self.id} for {name} ({self.frequency})"

    def is_scheduled_for_date(self, target_date) -> bool:
        """
        Check if recurring order is scheduled to run on target_date (date object):
        - False if paused
        - False if target_date in skipped_dates
        - True if daily
        - If weekdays: checks if target_date.weekday() in configured weekdays
        """
        if self.is_paused:
            return False
        date_str = target_date.isoformat()
        if date_str in (self.skipped_dates or []):
            return False
        if self.frequency == 'daily':
            return True
        if self.frequency == 'weekdays':
            if not self.weekdays:
                return True
            valid_days = [int(w.strip()) for w in self.weekdays.split(',') if w.strip().isdigit()]
            return target_date.weekday() in valid_days
        return False


class RecurringOrderItem(models.Model):
    recurring_order = models.ForeignKey(RecurringOrder, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"{self.quantity}x {self.product.name} (Recurring #{self.recurring_order_id})"


class RecurringOrderExecution(models.Model):
    """
    Audit and duplicate prevention ledger:
    - Exactly one order generated per (recurring_order, execution_date)
    """
    recurring_order = models.ForeignKey(RecurringOrder, on_delete=models.CASCADE, related_name='executions')
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='recurring_execution')
    execution_date = models.DateField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('recurring_order', 'execution_date')
        ordering = ['-execution_date']

    def __str__(self):
        return f"Execution of #{self.recurring_order_id} on {self.execution_date} -> Order #{self.order_id}"

