from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import DeviceTokenViewSet, NotificationLogViewSet

router = DefaultRouter()
router.register(r'devices', DeviceTokenViewSet, basename='device-token')
router.register(r'history', NotificationLogViewSet, basename='notification-history')

urlpatterns = [
    path('', include(router.urls)),
]
