from django.urls import path
from .views import RegisterView, CurrentUserProfileView, SendOtpView, ResendOtpView, VerifyOtpView

urlpatterns = [
    # Traditional registration & profile
    path('register/', RegisterView.as_view(), name='register'),
    path('profile/', CurrentUserProfileView.as_view(), name='current-user-profile'),

    # OTP Provider Endpoints
    path('otp/send/', SendOtpView.as_view(), name='otp-send'),
    path('otp/resend/', ResendOtpView.as_view(), name='otp-resend'),
    path('otp/verify/', VerifyOtpView.as_view(), name='otp-verify'),
]
