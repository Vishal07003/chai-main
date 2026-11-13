from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import OrderViewSet, PriceOverrideRequestViewSet

router = DefaultRouter()
router.register(r'price-overrides', PriceOverrideRequestViewSet, basename='price-override')
router.register(r'', OrderViewSet, basename='order')

urlpatterns = [
    path('', include(router.urls)),
]
