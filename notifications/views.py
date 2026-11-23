from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Q
from .models import DeviceToken, NotificationLog
from .serializers import DeviceTokenSerializer, NotificationLogSerializer
from .services import NotificationService

class DeviceTokenViewSet(viewsets.ModelViewSet):
    """
    Authenticated FCM device token management:
    - Register new FCM device token
    - Deactivate token on logout
    - Scoped strictly to request.user
    """
    serializer_class = DeviceTokenSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return DeviceToken.objects.filter(user=self.request.user)

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=['is_active'])

    @action(detail=False, methods=['post'])
    def deactivate(self, request):
        token = request.data.get('token')
        if token:
            DeviceToken.objects.filter(user=request.user, token=token).update(is_active=False)
            return Response({'status': 'token deactivated'})
        return Response({'error': 'Token required'}, status=status.HTTP_400_BAD_REQUEST)


class NotificationLogViewSet(viewsets.ModelViewSet):
    """
    Role-based Notification History & Delivery Status.
    Owner sees owner notifications and system logs.
    Delivery Partner sees delivery assignments and alerts.
    Supports authenticated deletion (swipe-to-delete) scoped strictly to user's permissions.
    """
    serializer_class = NotificationLogSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        user = self.request.user
        role = getattr(user.profile, 'role', 'customer') if hasattr(user, 'profile') else 'customer'

        if user.is_staff or user.is_superuser or role == 'owner':
            # Owner can inspect notifications sent to them, to role 'owner', or system logs
            return NotificationLog.objects.filter(
                Q(recipient=user) | Q(target_role__in=['owner', 'all'])
            )
        elif role == 'delivery':
            # Delivery Partner sees deliveries assigned & alerts sent to them or role 'delivery'
            return NotificationLog.objects.filter(
                Q(recipient=user) | Q(target_role__in=['delivery', 'all'])
            )
        else:
            return NotificationLog.objects.filter(recipient=user)

    def perform_destroy(self, instance):
        # Scoped strictly: user can only delete records that match their queryset
        instance.delete()

    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        instance = self.get_object()
        instance.is_read = True
        instance.save(update_fields=['is_read'])
        return Response({'success': True, 'is_read': True})

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        qs = self.get_queryset().filter(is_read=False)
        updated = qs.update(is_read=True)
        return Response({'success': True, 'updated_count': updated})

    @action(detail=False, methods=['post'])
    def retry_failed(self, request):
        """Only staff or business owners can trigger manual retry of failed notifications"""
        user = request.user
        role = getattr(user.profile, 'role', 'customer') if hasattr(user, 'profile') else 'customer'
        if not (user.is_staff or user.is_superuser or role == 'owner'):
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        retried = NotificationService.retry_failed_notifications(limit=25)
        return Response({'success': True, 'retried_count': retried})
