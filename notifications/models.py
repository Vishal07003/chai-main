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
