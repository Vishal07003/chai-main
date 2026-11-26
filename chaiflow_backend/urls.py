from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from django.conf import settings
from django.conf.urls.static import static
from rest_framework_simplejwt.views import TokenRefreshView
from accounts.views import (
    RoleTokenObtainPairView,
    UnifiedRequestOtpView,
    UnifiedVerifyOtpView,
    DeliveryRequestOtpView,
    DeliveryVerifyOtpView,
)

def health_check(request):
    return JsonResponse({'status': 'ok', 'service': 'chaiflow_api'})

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', health_check, name='api_health_check'),

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
    path('api/v1/reports/', include('reports.urls')),
]

if settings.DEBUG or True:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
