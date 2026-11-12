from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from .models import Order
from .serializers import OrderSerializer, OrderCreateSerializer

class OrderViewSet(viewsets.ModelViewSet):
    """
    Handles orders with role-based access:
    - Customers: can view and create their own orders; can cancel if still 'pending'.
    - Owners/Admins: full access to view, update statuses, manage all orders.
    - Delivery Partners: can view orders assigned to them.
    """
    queryset = Order.objects.all()
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'create':
            return OrderCreateSerializer
        return OrderSerializer

    def get_queryset(self):
        user = self.request.user
        queryset = Order.objects.select_related('customer').prefetch_related('items__product').order_by('-created_at')

        if not user or not user.is_authenticated:
            return Order.objects.none()

        if user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner'):
            status_param = self.request.query_params.get('status')
            if status_param:
                queryset = queryset.filter(status=status_param)
            return queryset

        if hasattr(user, 'profile') and user.profile.role == 'delivery':
            return queryset.filter(delivery__driver=user)

        # Standard customer sees only their own orders
        return queryset.filter(customer=user)

    def perform_update(self, serializer):
        user = self.request.user
        instance = serializer.instance
        is_owner = user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')

        if not is_owner:
            # Customers can only cancel their own pending orders
            new_status = self.request.data.get('status')
            if new_status == 'cancelled' and instance.status == 'pending':
                serializer.save(status='cancelled')
                return
            raise PermissionDenied("Only owners or admins can modify existing order statuses.")

        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        is_owner = user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')
        if not is_owner:
            raise PermissionDenied("Only owners or admins can delete orders.")
        instance.delete()
