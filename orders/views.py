from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from decimal import Decimal
from .models import Order, OrderItem, PriceOverrideRequest, RecurringOrder, RecurringOrderItem, RecurringOrderExecution
from .serializers import (
    OrderSerializer,
    OrderCreateSerializer,
    PriceOverrideRequestSerializer,
    RecurringOrderSerializer,
    RecurringOrderCreateUpdateSerializer,
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
            customer_id = self.request.query_params.get('customer')
            if status_param:
                queryset = queryset.filter(status=status_param)
            if company_id:
                queryset = queryset.filter(company_id=company_id)
            if customer_id:
                queryset = queryset.filter(Q(customer_profile_id=customer_id) | Q(customer_id=customer_id))
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


class RecurringOrderViewSet(viewsets.ModelViewSet):
    """
    Recurring Orders Management:
    - Template creation for regular customers (daily or specific weekdays)
    - Pause / Resume recurring schedule
    - Skip a specific date
    - Generate today's actual Order with duplicate prevention
    - Delivery partner assignment
    - Template edits apply to future orders only
    """
    queryset = RecurringOrder.objects.select_related(
        'customer', 'customer_profile', 'company', 'delivery_partner'
    ).prefetch_related('items__product', 'executions').order_by('-created_at')
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return RecurringOrderCreateUpdateSerializer
        return RecurringOrderSerializer

    def get_queryset(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return RecurringOrder.objects.none()

        is_owner = user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')
        qs = self.queryset
        if not is_owner:
            qs = qs.filter(customer=user)

        # Filters: customer, is_paused
        cust_id = self.request.query_params.get('customer')
        if cust_id:
            qs = qs.filter(customer_profile_id=cust_id)
        is_paused = self.request.query_params.get('is_paused')
        if is_paused is not None:
            qs = qs.filter(is_paused=(is_paused.lower() in ('true', '1')))
        return qs

    @action(detail=True, methods=['post'], url_path='pause')
    def pause(self, request, pk=None):
        rec_order = self.get_object()
        rec_order.is_paused = True
        rec_order.save(update_fields=['is_paused'])
        return Response({'status': 'paused', 'id': rec_order.id})

    @action(detail=True, methods=['post'], url_path='resume')
    def resume(self, request, pk=None):
        rec_order = self.get_object()
        rec_order.is_paused = False
        rec_order.save(update_fields=['is_paused'])
        return Response({'status': 'resumed', 'id': rec_order.id})

    @action(detail=True, methods=['post'], url_path='skip-date')
    def skip_date(self, request, pk=None):
        """
        Skip a specific date for this recurring order:
        Body: { "date": "YYYY-MM-DD" }
        """
        rec_order = self.get_object()
        date_str = request.data.get('date')
        if not date_str:
            date_str = timezone.now().date().isoformat()

        dates = list(rec_order.skipped_dates or [])
        if date_str not in dates:
            dates.append(date_str)
            rec_order.skipped_dates = dates
            rec_order.save(update_fields=['skipped_dates'])
        return Response({'status': 'skipped', 'date': date_str, 'skipped_dates': rec_order.skipped_dates})

    @action(detail=True, methods=['post'], url_path='unskip-date')
    def unskip_date(self, request, pk=None):
        """
        Remove a date from skipped dates:
        Body: { "date": "YYYY-MM-DD" }
        """
        rec_order = self.get_object()
        date_str = request.data.get('date')
        if not date_str:
            date_str = timezone.now().date().isoformat()

        dates = [d for d in (rec_order.skipped_dates or []) if d != date_str]
        rec_order.skipped_dates = dates
        rec_order.save(update_fields=['skipped_dates'])
        return Response({'status': 'unskipped', 'date': date_str, 'skipped_dates': rec_order.skipped_dates})

    @action(detail=True, methods=['post'], url_path='generate-order')
    def generate_order(self, request, pk=None):
        """
        Generates today's actual order from this template after Owner review.
        - Strictly prevents duplicates for the same (recurring_order, execution_date).
        - Resolves pricing via InvoicingService.resolve_item_unit_price.
        - Automatically creates associated Delivery container with assigned delivery partner.
        - Does NOT generate an invoice (invoice creation remains manual and owner initiated).
        """
        rec_order = self.get_object()
        date_param = request.data.get('date')
        if date_param:
            import datetime
            try:
                target_date = datetime.date.fromisoformat(date_param)
            except ValueError:
                raise ValidationError({'date': 'Invalid date format. Expected YYYY-MM-DD.'})
        else:
            target_date = timezone.now().date()

        # 1. Duplicate check
        existing_exec = RecurringOrderExecution.objects.filter(
            recurring_order=rec_order,
            execution_date=target_date
        ).select_related('order').first()

        if existing_exec:
            return Response({
                'already_generated': True,
                'message': f"Order #{existing_exec.order_id} has already been generated for {target_date}.",
                'order': OrderSerializer(existing_exec.order).data
            }, status=status.HTTP_200_OK)

        # 2. Check items
        items = rec_order.items.select_related('product').all()
        if not items.exists():
            raise ValidationError({'detail': 'Recurring order template has no items.'})

        # 3. Create actual order atomically
        from django.db import transaction
        from billing.invoicing_service import InvoicingService
        from deliveries.models import Delivery
        from notifications.services import NotificationService

        pricing_entity = rec_order.customer_profile or rec_order.company

        with transaction.atomic():
            total = Decimal('0.00')
            order = Order.objects.create(
                customer=rec_order.customer,
                customer_profile=rec_order.customer_profile,
                company=rec_order.company,
                status='pending',
                total_amount=Decimal('0.00'),
                delivery_address=rec_order.delivery_address,
                notes=rec_order.notes or f"Generated from Recurring Order #{rec_order.id}",
            )

            for it in items:
                # Use snapshot agreed unit price if specified on recurring template, otherwise resolve rate
                if it.unit_price and it.unit_price > 0:
                    order_item_price = it.unit_price
                else:
                    order_item_price = InvoicingService.resolve_item_unit_price(
                        pricing_entity, it.product, it.quantity
                    )
                OrderItem.objects.create(
                    order=order,
                    product=it.product,
                    quantity=it.quantity,
                    price=order_item_price
                )
                total += (order_item_price * it.quantity)

            order.total_amount = total
            order.save(update_fields=['total_amount'])

            # Create delivery assignment
            driver_user = rec_order.delivery_partner.user if (rec_order.delivery_partner and rec_order.delivery_partner.user) else None
            delivery = Delivery.objects.create(
                order=order,
                driver=driver_user,
                status='assigned'
            )
            if driver_user:
                NotificationService.on_delivery_assigned(delivery)

            # Record execution ledger for duplicate prevention
            exec_record = RecurringOrderExecution.objects.create(
                recurring_order=rec_order,
                order=order,
                execution_date=target_date
            )

            # Notify Owner of new order
            NotificationService.on_order_created(order)

        return Response({
            'success': True,
            'message': f"Order #{order.id} generated successfully for {target_date}.",
            'order': OrderSerializer(order).data,
            'execution_id': exec_record.id,
        }, status=status.HTTP_201_CREATED)

