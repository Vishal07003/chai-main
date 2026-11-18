from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import OrderViewSet, PriceOverrideRequestViewSet, RecurringOrderViewSet

router = DefaultRouter()
router.register(r'price-overrides', PriceOverrideRequestViewSet, basename='price-override')
router.register(r'recurring', RecurringOrderViewSet, basename='recurring-order')
router.register(r'', OrderViewSet, basename='order')

urlpatterns = [
    path('', include(router.urls)),
]
