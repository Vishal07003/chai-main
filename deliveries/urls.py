from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import DeliveryViewSet, DeliveryPartnerViewSet, DeliveryScheduleViewSet

router = DefaultRouter()
router.register(r'partners', DeliveryPartnerViewSet, basename='delivery-partner')
router.register(r'schedules', DeliveryScheduleViewSet, basename='delivery-schedule')
router.register(r'', DeliveryViewSet, basename='delivery-item')

urlpatterns = [
    path('', include(router.urls)),
]
