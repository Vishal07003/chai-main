from django.contrib import admin
from django.urls import path, include
from rest_framework_simplejwt.views import TokenRefreshView
from accounts.views import (
    RoleTokenObtainPairView,
    UnifiedRequestOtpView,
    UnifiedVerifyOtpView,
    DeliveryRequestOtpView,
    DeliveryVerifyOtpView,
)

urlpatterns = [
    path('admin/', admin.site.urls),

    # Single canonical declaration of JWT Authentication with strict role validation
    path('api/v1/auth/token/', RoleTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/v1/auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    # UNIFIED SINGLE MOBILE OTP LOGIN (Automatic role detection)
    path('api/v1/auth/request-otp/', UnifiedRequestOtpView.as_view(), name='unified_request_otp'),
    path('api/v1/auth/verify-otp/', UnifiedVerifyOtpView.as_view(), name='unified_verify_otp'),

    # Dedicated Delivery Partner Endpoints (aliases for compatibility)
    path('api/v1/auth/delivery/request-otp/', DeliveryRequestOtpView.as_view(), name='delivery_request_otp'),
    path('api/v1/auth/delivery/verify-otp/', DeliveryVerifyOtpView.as_view(), name='delivery_verify_otp'),

    # Chai Flow Modules
    path('api/v1/accounts/', include('accounts.urls')),
    path('api/v1/customers/', include('billing.customer_urls')),
    path('api/v1/products/', include('products.urls')),
    path('api/v1/orders/', include('orders.urls')),
    path('api/v1/deliveries/', include('deliveries.urls')),
    path('api/v1/billing/', include('billing.urls')),
    path('api/v1/notifications/', include('notifications.urls')),
]
