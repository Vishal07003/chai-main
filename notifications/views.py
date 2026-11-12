from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import DeviceToken
from .serializers import DeviceTokenSerializer

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
        # Soft-deactivate token on logout rather than hard-deleting
        instance.is_active = False
        instance.save(update_fields=['is_active'])

    @action(detail=False, methods=['post'])
    def deactivate(self, request):
        token = request.data.get('token')
        if token:
            DeviceToken.objects.filter(user=request.user, token=token).update(is_active=False)
            return Response({'status': 'token deactivated'})
        return Response({'error': 'Token required'}, status=status.HTTP_400_BAD_REQUEST)
