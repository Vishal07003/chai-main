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
    Tracks OTP dispatch sessions and verification state per mobile number.
    Implements security controls:
    - 6-digit cryptographically secure OTP
    - Hashed OTP storage
    - Max 5 verification attempts
    - 5-minute expiry
    - Single-use invalidate on success or new issue
    """
    phone = models.CharField(max_length=15, db_index=True)
    session_id = models.CharField(max_length=120, unique=True, db_index=True)
    otp_hash = models.CharField(max_length=128, blank=True, null=True)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    @property
    def has_exceeded_attempts(self) -> bool:
        return self.attempts >= self.max_attempts

    def __str__(self):
        return f"OTP Session {self.session_id} for {self.phone} [Verified: {self.is_verified}, Attempts: {self.attempts}/{self.max_attempts}]"
