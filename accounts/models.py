from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

class UserProfile(models.Model):
    ROLE_CHOICES = (
        ('owner', 'Owner'),
        ('customer', 'Customer'),
        ('delivery', 'Delivery Partner'),
    )

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    phone = models.CharField(max_length=15, unique=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='customer')
    address = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} ({self.role})"

class OtpSession(models.Model):
    """
    Tracks OTP dispatch sessions and verification state per mobile number
    """
    phone = models.CharField(max_length=15, db_index=True)
    session_id = models.CharField(max_length=120, unique=True, db_index=True)
    mock_otp = models.CharField(max_length=10, blank=True, null=True)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"OTP Session {self.session_id} for {self.phone} [Verified: {self.is_verified}]"
