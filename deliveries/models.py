from django.db import models
from django.contrib.auth.models import User
from orders.models import Order
from products.models import Product

class Delivery(models.Model):
    STATUS_CHOICES = (
        ('assigned', 'Assigned'),
        ('accepted', 'Accepted'),
        ('picked_up', 'Picked Up'),
        ('on_the_way', 'On The Way'),
        ('delivered', 'Delivered'),
        ('failed', 'Failed'),
    )

    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='delivery')
    driver = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_deliveries')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='assigned')
    pickup_time = models.DateTimeField(null=True, blank=True)
    delivered_time = models.DateTimeField(null=True, blank=True)
    current_lat = models.FloatField(null=True, blank=True)
    current_lng = models.FloatField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Delivery for Order #{self.order.id} ({self.status})"


class DeliveryItem(models.Model):
    """
    Records actual quantities delivered by the partner.
    Used during Owner manual invoice review to audit discrepancies.
    """
    delivery = models.ForeignKey(Delivery, on_delete=models.CASCADE, related_name='delivered_items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True)
    ordered_quantity = models.PositiveIntegerField(default=1)
    delivered_quantity = models.PositiveIntegerField(default=0)
    discrepancy_notes = models.TextField(blank=True, default='')

    def __str__(self):
        return f"DeliveryItem #{self.id}: {self.delivered_quantity}/{self.ordered_quantity} of {self.product.name if self.product else 'Item'}"

    @property
    def has_discrepancy(self) -> bool:
        return self.delivered_quantity != self.ordered_quantity


class DeliveryPartner(models.Model):
    """
    Fleet Partner Profile (for AddDeliveryBoyScreen, RouteBoysScreen)
    """
    STATUS_CHOICES = (
        ('READY', 'Ready'),
        ('BUSY', 'Busy'),
        ('OFFLINE', 'Offline'),
    )
    VEHICLE_CHOICES = (
        ('Scooter', 'Scooter'),
        ('E-Bike', 'E-Bike'),
        ('Auto Cargo', 'Auto Cargo'),
        ('Mini Van', 'Mini Van'),
    )

    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='partner_profile')
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=15, unique=True, db_index=True)
    emergency_phone = models.CharField(max_length=15, blank=True, default='')
    license_number = models.CharField(max_length=50, blank=True, default='')
    vehicle_type = models.CharField(max_length=30, choices=VEHICLE_CHOICES, default='Scooter')
    vehicle_model = models.CharField(max_length=100, blank=True, default='')
    number_plate = models.CharField(max_length=30, blank=True, default='')
    location = models.CharField(max_length=200, blank=True, default='Hub Central')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='READY')
    avatar_url = models.URLField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.vehicle_type} - {self.status})"


class DeliverySchedule(models.Model):
    """
    Delivery Route Schedule (for ScheduleScreen, AddRouteScheduleScreen)
    """
    STATUS_CHOICES = (
        ('UPCOMING', 'Upcoming'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
    )

    time_slot = models.CharField(max_length=80, default='07:00 AM - 09:00 AM')
    title = models.CharField(max_length=200)
    customer_name = models.CharField(max_length=200)
    address = models.TextField()
    partner = models.ForeignKey(DeliveryPartner, on_delete=models.SET_NULL, null=True, blank=True, related_name='schedules')
    partner_name = models.CharField(max_length=150, default='Unassigned')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='UPCOMING')
    items_count = models.PositiveIntegerField(default=1)
    scheduled_date = models.DateField(auto_now_add=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-scheduled_date', 'time_slot']

    def __str__(self):
        return f"{self.title} ({self.time_slot}) - {self.partner_name} [{self.status}]"
