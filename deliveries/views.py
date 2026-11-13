from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from .models import (
    Delivery,
    DeliveryItem,
    DeliveryPartner,
    DeliverySchedule,
    DeliveryLocationLog,
    DeliveryStatusHistory,
)
from .serializers import (
    DeliverySerializer,
    DeliveryItemSerializer,
    DeliveryPartnerSerializer,
    DeliveryScheduleSerializer,
    DeliveryLocationLogSerializer,
    DeliveryStatusHistorySerializer,
)

class DeliveryViewSet(viewsets.ModelViewSet):
    """
    Delivery management:
    - Owners/Staff: view all deliveries, assign/reassign drivers, view tracking history.
    - Drivers: view ONLY deliveries assigned to them; update delivery status, recorded quantities, and location coordinates.
    - Lifecycle: assigned -> accepted -> picked_up -> on_the_way -> delivered / failed.
    """
    queryset = Delivery.objects.all()
    serializer_class = DeliverySerializer
    permission_classes = [permissions.IsAuthenticated]

    ALLOWED_STATUS_TRANSITIONS = {
        'assigned': ['accepted', 'picked_up', 'failed'],
        'accepted': ['picked_up', 'failed'],
        'picked_up': ['on_the_way', 'delivered', 'failed'],
        'on_the_way': ['delivered', 'failed'],
        'delivered': [],
        'failed': [],
    }

    def get_queryset(self):
        user = self.request.user
        queryset = Delivery.objects.select_related('order', 'order__customer', 'order__company', 'driver', 'driver__profile').prefetch_related('delivered_items', 'status_history').order_by('-updated_at')

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

        return queryset.filter(order__customer=user)

    def perform_update(self, serializer):
        user = self.request.user
        instance = serializer.instance
        is_owner = user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')
        is_assigned_driver = instance.driver == user

        if not is_owner and not is_assigned_driver:
            raise PermissionDenied("You do not have permission to modify this delivery.")

        if not is_owner and is_assigned_driver:
            if 'driver' in self.request.data:
                raise PermissionDenied("Drivers cannot reassign deliveries.")

        new_status = self.request.data.get('status')
        current_status = instance.status

        # Validate status transitions
        if new_status and new_status != current_status and not is_owner:
            allowed = self.ALLOWED_STATUS_TRANSITIONS.get(current_status, [])
            if new_status not in allowed:
                raise ValidationError({
                    'status': f"Invalid status transition from '{current_status}' to '{new_status}'. Allowed: {allowed}"
                })

        extra_fields = {}
        if new_status == 'picked_up' and not instance.pickup_time:
            extra_fields['pickup_time'] = timezone.now()
        elif new_status == 'delivered' and not instance.delivered_time:
            extra_fields['delivered_time'] = timezone.now()

        previous_driver = instance.driver
        updated_delivery = serializer.save(**extra_fields)

        # Audit log state transition
        if new_status and new_status != current_status:
            DeliveryStatusHistory.objects.create(
                delivery=updated_delivery,
                from_status=current_status,
                to_status=new_status,
                changed_by=user,
                notes=self.request.data.get('notes', '')
            )

        # Trigger FCM notification when driver is assigned or changed
        if updated_delivery.driver and updated_delivery.driver != previous_driver:
            from notifications.services import NotificationService
            NotificationService.on_delivery_assigned(updated_delivery)

    def perform_destroy(self, instance):
        user = self.request.user
        is_owner = user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')
        if is_owner:
            instance.delete()
            return
        raise PermissionDenied("Only owners can delete delivery records.")

    @action(detail=True, methods=['post'], url_path='location-ping')
    def location_ping(self, request, pk=None):
        """
        Record real-time or buffered GPS coordinates from the assigned delivery partner.
        Accepts single ping or array of buffered offline pings:
        Body: { "latitude": 12.9716, "longitude": 77.5946, "accuracy": 5.0, "speed": 15.2, "recorded_at": "ISO" }
        or: { "pings": [ {...}, {...} ] }
        """
        delivery = self.get_object()
        user = request.user
        is_owner = user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')

        if not is_owner and delivery.driver != user:
            raise PermissionDenied("Only the assigned driver can send location pings for this delivery.")

        # Ignore location pings if delivery is already delivered or failed (privacy rule)
        if delivery.status in ('delivered', 'failed'):
            return Response({
                'message': f"Delivery is '{delivery.status}'. Location updates are terminated.",
                'active_tracking': False,
            })

        pings_data = request.data.get('pings')
        if not pings_data:
            pings_data = [request.data]

        saved_logs = []
        latest_lat = None
        latest_lng = None

        for p in pings_data:
            lat = p.get('latitude')
            lng = p.get('longitude')
            if lat is None or lng is None:
                continue

            rec_time_str = p.get('recorded_at')
            rec_time = timezone.now()
            if rec_time_str:
                try:
                    from dateutil.parser import parse
                    rec_time = parse(rec_time_str)
                    if timezone.is_naive(rec_time):
                        rec_time = timezone.make_aware(rec_time)
                except Exception:
                    rec_time = timezone.now()

            log = DeliveryLocationLog.objects.create(
                delivery=delivery,
                driver=user,
                latitude=float(lat),
                longitude=float(lng),
                accuracy=p.get('accuracy'),
                speed=p.get('speed'),
                recorded_at=rec_time,
            )
            saved_logs.append(log)
            latest_lat = float(lat)
            latest_lng = float(lng)

        if latest_lat is not None and latest_lng is not None:
            delivery.current_lat = latest_lat
            delivery.current_lng = latest_lng
            delivery.save(update_fields=['current_lat', 'current_lng', 'updated_at'])

        return Response({
            'success': True,
            'pings_recorded': len(saved_logs),
            'current_lat': latest_lat,
            'current_lng': latest_lng,
            'active_tracking': True,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='record-delivered-quantities')
    def record_delivered_quantities(self, request, pk=None):
        """
        Record actual delivered quantities before marking delivered.
        Body: { "items": [ { "product_id": 1, "delivered_quantity": 28, "discrepancy_notes": "2 spilled" } ] }
        """
        delivery = self.get_object()
        user = request.user
        is_owner = user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')

        if not is_owner and delivery.driver != user:
            raise PermissionDenied("Only the assigned driver or owner can record delivered quantities.")

        items_payload = request.data.get('items', [])
        saved_items = []

        for item_data in items_payload:
            pid = item_data.get('product_id')
            delivered_qty = int(item_data.get('delivered_quantity', 0))
            notes = item_data.get('discrepancy_notes', '')

            # Match with order item ordered quantity
            order_item = delivery.order.items.filter(product_id=pid).first()
            ordered_qty = order_item.quantity if order_item else delivered_qty

            d_item, _ = DeliveryItem.objects.update_or_create(
                delivery=delivery,
                product_id=pid,
                defaults={
                    'ordered_quantity': ordered_qty,
                    'delivered_quantity': delivered_qty,
                    'discrepancy_notes': notes,
                }
            )
            saved_items.append(d_item)

        return Response(DeliveryItemSerializer(saved_items, many=True).data)


class DeliveryPartnerViewSet(viewsets.ModelViewSet):
    """
    Fleet Partner CRUD for Owners and driver profile monitoring
    """
    queryset = DeliveryPartner.objects.all()
    serializer_class = DeliveryPartnerSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = super().get_queryset()

        if user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner'):
            status_param = self.request.query_params.get('status')
            if status_param:
                queryset = queryset.filter(status=status_param.upper())
            return queryset

        # Driver viewing own partner profile
        if hasattr(user, 'profile') and user.profile.role == 'delivery':
            return queryset.filter(user=user)

        return queryset.none()


class DeliveryScheduleViewSet(viewsets.ModelViewSet):
    """
    Route and delivery schedule management
    """
    queryset = DeliverySchedule.objects.all()
    serializer_class = DeliveryScheduleSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset()
        status_param = self.request.query_params.get('status')
        date_param = self.request.query_params.get('date')
        if status_param:
            queryset = queryset.filter(status=status_param.upper())
        if date_param:
            queryset = queryset.filter(scheduled_date=date_param)
        return queryset
