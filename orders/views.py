from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from .models import Order
from .serializers import OrderSerializer, OrderCreateSerializer

class OrderViewSet(viewsets.ModelViewSet):
    """
    Handles orders with role-based access and strict state machine transitions:
    - Customers: can view and create their own orders; can ONLY cancel if still 'pending'.
    - Owners/Admins: full access to view, update statuses following the state machine, manage all orders.
    - Delivery Partners: can view orders assigned to them.
    """
    queryset = Order.objects.all()
    permission_classes = [permissions.IsAuthenticated]

    # Permitted state transitions for order lifecycle
    ALLOWED_TRANSITIONS = {
        'pending': ['accepted', 'cancelled'],
        'accepted': ['preparing', 'cancelled'],
        'preparing': ['out_for_delivery', 'cancelled'],
        'out_for_delivery': ['delivered', 'cancelled'],
        'delivered': [],
        'cancelled': [],
    }

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

        # Prevent changing immutable fields: customer, items, total_amount
        if 'customer' in self.request.data:
            raise PermissionDenied("Order customer cannot be reassigned.")

        new_status = self.request.data.get('status')
        current_status = instance.status

        if not is_owner:
            # Customers are only allowed to cancel their own orders, and only while status is 'pending'
            if new_status == 'cancelled':
                if current_status != 'pending':
                    raise ValidationError({
                        'status': f"Cannot cancel order in '{current_status}' status. Only pending orders can be cancelled."
                    })
                serializer.save(status='cancelled')
                return
            raise PermissionDenied("Customers can only cancel pending orders. Status cannot be modified.")

        # Owner/Staff status transitions enforcement
        if new_status and new_status != current_status:
            allowed = self.ALLOWED_TRANSITIONS.get(current_status, [])
            if new_status not in allowed:
                raise ValidationError({
                    'status': f"Invalid status transition from '{current_status}' to '{new_status}'. Allowed transitions: {allowed}"
                })

        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        is_owner = user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')
        if not is_owner:
            raise PermissionDenied("Only owners or admins can delete orders.")
        instance.delete()
