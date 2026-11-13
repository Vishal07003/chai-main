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
