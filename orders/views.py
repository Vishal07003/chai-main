from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from .models import Order, OrderItem, PriceOverrideRequest
from .serializers import (
    OrderSerializer,
    OrderCreateSerializer,
    PriceOverrideRequestSerializer,
)
from billing.invoicing_service import InvoicingService
from billing.serializers import InvoiceDetailSerializer
from accounts.permissions import IsOwnerUser

class OrderViewSet(viewsets.ModelViewSet):
    """
    Handles orders with role-based access and strict state machine transitions:
    - Customers: can view and create their own orders; can ONLY cancel if still 'pending'.
    - Owners/Admins: full access to view, update statuses following the state machine, manage all orders.
    - Delivery Partners: can view orders assigned to them.
    - Manual Owner Invoicing: invoice_preview and generate_invoice actions.
    """
    queryset = Order.objects.all()
    permission_classes = [permissions.IsAuthenticated]

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
        queryset = Order.objects.select_related('customer', 'company').prefetch_related('items__product').order_by('-created_at')

        if not user or not user.is_authenticated:
            return Order.objects.none()

        if user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner'):
            status_param = self.request.query_params.get('status')
            company_id = self.request.query_params.get('company')
            if status_param:
                queryset = queryset.filter(status=status_param)
            if company_id:
                queryset = queryset.filter(company_id=company_id)
            return queryset

        if hasattr(user, 'profile') and user.profile.role == 'delivery':
            return queryset.filter(delivery__driver=user)

        return queryset.filter(customer=user)

    def perform_update(self, serializer):
        user = self.request.user
        instance = serializer.instance
        is_owner = user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')

        if 'customer' in self.request.data:
            raise PermissionDenied("Order customer cannot be reassigned.")

        new_status = self.request.data.get('status')
        current_status = instance.status

        if not is_owner:
            if new_status == 'cancelled':
                if current_status != 'pending':
                    raise ValidationError({
                        'status': f"Cannot cancel order in '{current_status}' status. Only pending orders can be cancelled."
                    })
                serializer.save(status='cancelled')
                return
            raise PermissionDenied("Customers can only cancel pending orders. Status cannot be modified.")

        if new_status and new_status != current_status:
            allowed = self.ALLOWED_TRANSITIONS.get(current_status, [])
            if new_status not in allowed:
                raise ValidationError({
                    'status': f"Invalid status transition from '{current_status}' to '{new_status}'. Allowed transitions: {allowed}"
                })

        instance = serializer.save()

        if new_status and new_status != current_status:
            from notifications.services import NotificationService
            NotificationService.on_order_status_updated(instance)

    def perform_destroy(self, instance):
        user = self.request.user
        is_owner = user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')
        if not is_owner:
            raise PermissionDenied("Only owners or admins can delete orders.")
        if instance.has_invoice:
            raise ValidationError({'error': 'Cannot delete order that has an associated official invoice.'})
        instance.delete()

    @action(detail=True, methods=['get'], url_path='invoice-preview')
    def invoice_preview(self, request, pk=None):
        """
        Owner Review Screen before generating manual invoice:
        - Displays company, ordered quantities, delivered quantities (from DeliveryItem)
        - Highlights discrepancies
        - Recalculates unit prices and approved discounts on the backend
        """
        order = self.get_object()
        user = request.user
        if not (user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')):
            raise PermissionDenied("Only the owner can preview invoice generation.")

        company = order.company
        delivery = getattr(order, 'delivery', None)
        delivered_map = {}
        if delivery:
            for d_item in delivery.delivered_items.all():
                if d_item.product_id:
                    delivered_map[d_item.product_id] = d_item.delivered_quantity

        items_preview = []
        subtotal = Decimal('0.00')
        total_discount = Decimal('0.00')

        for item in order.items.select_related('product').all():
            p = item.product
            ordered_qty = item.quantity
            delivered_qty = delivered_map.get(p.id, ordered_qty) if delivery else ordered_qty
            has_discrepancy = (delivered_qty != ordered_qty)

            # Resolve base price
            base_price = InvoicingService.resolve_item_unit_price(company, p, delivered_qty)

            # Check approved discount
            override = PriceOverrideRequest.objects.filter(
                order=order, product=p, status='APPROVED'
            ).order_by('-decision_timestamp').first()

            effective_price = override.requested_price if override else base_price
            discount_per_unit = max(Decimal('0.00'), base_price - effective_price)
            line_total = effective_price * delivered_qty

            subtotal += (base_price * delivered_qty)
            total_discount += (discount_per_unit * delivered_qty)

            items_preview.append({
                'product_id': p.id,
                'product_name': p.name,
                'ordered_quantity': ordered_qty,
                'delivered_quantity': delivered_qty,
                'has_discrepancy': has_discrepancy,
                'standard_rate': str(base_price),
                'approved_discount_per_unit': str(discount_per_unit),
                'effective_rate': str(effective_price),
                'recommended_quantity': delivered_qty,
                'line_total': str(line_total),
            })

        grand_total = max(Decimal('0.00'), subtotal - total_discount)

        return Response({
            'order_id': order.id,
            'company': {
                'id': company.id if company else None,
                'name': company.name if company else order.customer.username,
                'phone': company.phone if company else getattr(order.customer.profile, 'phone', ''),
            },
            'has_existing_invoice': order.has_invoice,
            'existing_invoice_number': order.invoice.invoice_number if order.has_invoice else None,
            'delivery_status': delivery.status if delivery else 'No delivery assigned',
            'items': items_preview,
            'subtotal': str(subtotal),
            'discount_total': str(total_discount),
            'grand_total': str(grand_total),
        })

    @action(detail=True, methods=['post'], url_path='generate-invoice')
    def generate_invoice(self, request, pk=None):
        """
        Explicit Manual Invoice Generation by Owner:
        - Never automatically called upon order or delivery creation.
        - Idempotent: enforces 1:1 uniqueness (HTTP 409 Conflict if already exists).
        - Validates owner-confirmed quantities and recalculates all totals on backend.
        """
        order = self.get_object()
        user = request.user
        if not (user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')):
            raise PermissionDenied("Only the owner can generate official invoices.")

        if order.has_invoice:
            return Response({
                'error': f"Invoice #{order.invoice.invoice_number} already exists for this order.",
                'invoice_id': order.invoice.id,
                'invoice_number': order.invoice.invoice_number,
            }, status=status.HTTP_409_CONFLICT)

        items_payload = request.data.get('items', [])
        notes = request.data.get('notes', '')

        # If items not provided, default to order's actual delivered quantities
        if not items_payload:
            delivery = getattr(order, 'delivery', None)
            delivered_map = {}
            if delivery:
                for d in delivery.delivered_items.all():
                    if d.product_id:
                        delivered_map[d.product_id] = d.delivered_quantity

            items_payload = [
                {
                    'product_id': it.product_id,
                    'quantity': delivered_map.get(it.product_id, it.quantity)
                }
                for it in order.items.all() if it.product_id
            ]

        invoice = InvoicingService.generate_manual_invoice(
            order=order,
            confirmed_items=items_payload,
            owner_user=user,
            notes=notes
        )

        return Response(InvoiceDetailSerializer(invoice).data, status=status.HTTP_201_CREATED)


class PriceOverrideRequestViewSet(viewsets.ModelViewSet):
    """
    Owner-Only Discount and Price Override approval queue:
    - View pending requests
    - Approve or reject with review notes
    """
    queryset = PriceOverrideRequest.objects.select_related('order', 'product', 'requested_by', 'reviewed_by').all().order_by('-created_at')
    serializer_class = PriceOverrideRequestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner'):
            status_param = self.request.query_params.get('status')
            if status_param:
                return self.queryset.filter(status=status_param.upper())
            return self.queryset
        return self.queryset.filter(requested_by=user)

    @action(detail=True, methods=['post'])
    def review(self, request, pk=None):
        """
        Owner reviews price override request:
        Body: { "decision": "APPROVED" | "REJECTED", "notes": "..." }
        """
        override_request = self.get_object()
        user = request.user
        if not (user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')):
            raise PermissionDenied("Only the owner can approve or reject price overrides.")

        decision = request.data.get('decision', '').upper()
        if decision not in ('APPROVED', 'REJECTED'):
            raise ValidationError({'decision': "Decision must be 'APPROVED' or 'REJECTED'."})

        notes = request.data.get('notes', '')

        override_request.status = decision
        override_request.reviewed_by = user
        override_request.decision_timestamp = timezone.now()
        override_request.review_notes = notes
        override_request.save(update_fields=['status', 'reviewed_by', 'decision_timestamp', 'review_notes'])

        return Response(PriceOverrideRequestSerializer(override_request).data)
