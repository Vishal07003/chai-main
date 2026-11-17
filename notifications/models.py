from django.db import models
from django.contrib.auth.models import User

class DeviceToken(models.Model):
    PLATFORM_CHOICES = (
        ('android', 'Android'),
        ('ios', 'iOS'),
        ('web', 'Web'),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='fcm_devices')
    token = models.CharField(max_length=255, unique=True, db_index=True)
    platform = models.CharField(max_length=10, choices=PLATFORM_CHOICES, default='android')
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.user.username} ({self.platform}): {self.token[:15]}... [Active: {self.is_active}]"


class NotificationLog(models.Model):
    """
    Centralized Notification Log & Delivery History.
    Stores FCM & SMS notification dispatch history, delivery status, and retry attempts.
    Strictly excludes WhatsApp.
    """
    CHANNEL_CHOICES = (
        ('fcm', 'Firebase Cloud Messaging'),
        ('sms', 'SMS Gateway'),
    )

    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    )

    TARGET_ROLE_CHOICES = (
        ('owner', 'Business Owner'),
        ('delivery', 'Delivery Partner'),
        ('customer', 'Customer'),
        ('all', 'All Roles'),
    )

    recipient = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='notifications_received',
        null=True,
        blank=True
    )
    target_role = models.CharField(
        max_length=20,
        choices=TARGET_ROLE_CHOICES,
        default='all',
        db_index=True
    )
    channel = models.CharField(
        max_length=10,
        choices=CHANNEL_CHOICES,
        default='fcm',
        db_index=True
    )
    title = models.CharField(max_length=255)
    body = models.TextField()
    data_payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True
    )
    is_read = models.BooleanField(default=False, db_index=True)
    error_message = models.TextField(blank=True, null=True)
    retry_count = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=3)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        recipient_name = self.recipient.username if self.recipient else f"Role:{self.target_role}"
        return f"[{self.get_channel_display()}] {self.title} -> {recipient_name} ({self.status})"
