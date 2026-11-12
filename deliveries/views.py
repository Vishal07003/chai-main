from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from django.utils import timezone
from .models import Delivery
from .serializers import DeliverySerializer

class DeliveryViewSet(viewsets.ModelViewSet):
    """
    Delivery management:
    - Owners/Staff: view all deliveries, assign drivers, update any delivery.
    - Drivers: view ONLY deliveries assigned to them; update delivery status and GPS coordinates.
    - Customers: view delivery info only for their own orders (read-only).
    """
    queryset = Delivery.objects.all()
    serializer_class = DeliverySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Delivery.objects.select_related('order', 'order__customer', 'driver', 'driver__profile').order_by('-updated_at')

        if not user or not user.is_authenticated:
            return Delivery.objects.none()

        if user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner'):
            driver_id = self.request.query_params.get('driver')
            status_param = self.request.query_params.get('status')
            if driver_id:
                queryset = queryset.filter(driver_id=driver_id)
            if status_param:
                queryset = queryset.filter(status=status_param)
            return queryset

        if hasattr(user, 'profile') and user.profile.role == 'delivery':
            return queryset.filter(driver=user)

        # Customer viewing deliveries for their orders
        return queryset.filter(order__customer=user)

    def perform_update(self, serializer):
        user = self.request.user
        instance = serializer.instance
        is_owner = user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')
        is_assigned_driver = instance.driver == user

        if not is_owner and not is_assigned_driver:
            raise PermissionDenied("You do not have permission to modify this delivery.")

        # If driver is updating, prevent driver from reassigning the delivery to someone else
        if not is_owner and is_assigned_driver:
            if 'driver' in self.request.data:
                raise PermissionDenied("Drivers cannot reassign deliveries.")

        # Automatically update timestamps when statuses change
        new_status = self.request.data.get('status')
        extra_fields = {}
        if new_status == 'picked_up' and not instance.pickup_time:
            extra_fields['pickup_time'] = timezone.now()
        elif new_status == 'delivered' and not instance.delivered_time:
            extra_fields['delivered_time'] = timezone.now()

        previous_driver = instance.driver
        updated_delivery = serializer.save(**extra_fields)

        # Trigger FCM notification when driver is assigned or changed
        if updated_delivery.driver and updated_delivery.driver != previous_driver:
            from notifications.services import NotificationService
            NotificationService.on_delivery_assigned(updated_delivery)

    def perform_destroy(self, instance):
        user = self.request.user
        is_owner = user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')
        if not is_owner:
            raise PermissionDenied("Only owners can delete delivery records.")
        instance.delete()
