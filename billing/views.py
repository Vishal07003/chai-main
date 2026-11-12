from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied
from django.db.models import Q, Sum, Count
from django.utils import timezone
from decimal import Decimal

from .models import Customer, Invoice, Payment
from .serializers import (
    CustomerSerializer,
    InvoiceDetailSerializer,
    InvoiceCreateSerializer,
    PaymentSerializer,
)
from accounts.permissions import IsOwnerUser

class CustomerViewSet(viewsets.ModelViewSet):
    """
    Customer management for stall owners:
    - Search by name or phone
    - List complete customer records with aggregated bills and totals
    - View customer invoices
    """
    queryset = Customer.objects.all().order_by('-created_at')
    serializer_class = CustomerSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset()
        search = self.request.query_params.get('search', '').strip()
        if search:
            queryset = queryset.filter(Q(name__icontains=search) | Q(phone__icontains=search))
        return queryset

    @action(detail=True, methods=['get'])
    def invoices(self, request, pk=None):
        customer = self.get_object()
        invoices = customer.invoices.all().prefetch_related('items', 'payments')

        date_param = request.query_params.get('date')
        if date_param:
            invoices = invoices.filter(created_at__date=date_param)

        serializer = InvoiceDetailSerializer(invoices, many=True)
        return Response(serializer.data)

class InvoiceViewSet(viewsets.ModelViewSet):
    """
    Quick Billing and Invoice Management:
    - Create invoice atomically with items & optional upfront payment
    - View invoice details with items, total_paid, pending_amount, payments
    - Add subsequent payment to invoice
    - Filter invoices by date, customer, payment status
    - Dashboard metrics aggregation
    """
    queryset = Invoice.objects.select_related('customer').prefetch_related('items', 'payments').all().order_by('-created_at')
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'create':
            return InvoiceCreateSerializer
        return InvoiceDetailSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        status_param = self.request.query_params.get('status')
        date_param = self.request.query_params.get('date')
        customer_id = self.request.query_params.get('customer')
        search = self.request.query_params.get('search', '').strip()

        if status_param:
            queryset = queryset.filter(status=status_param)
        if date_param:
            queryset = queryset.filter(created_at__date=date_param)
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        if search:
            queryset = queryset.filter(
                Q(invoice_number__icontains=search) |
                Q(customer_name__icontains=search) |
                Q(customer_phone__icontains=search)
            )

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = serializer.save()
        return Response(InvoiceDetailSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def payments(self, request, pk=None):
        invoice = self.get_object()
        amount_str = request.data.get('amount')
        method = request.data.get('method', 'cash')
        notes = request.data.get('notes', '')

        try:
            amount = Decimal(str(amount_str))
        except (ValueError, TypeError):
            raise ValidationError({'amount': 'Valid payment amount is required.'})

        if amount <= Decimal('0.00'):
            raise ValidationError({'amount': 'Payment amount must be greater than zero.'})

        remaining_due = invoice.pending_amount
        if amount > remaining_due:
            raise ValidationError({
                'amount': f"Payment amount (₹{amount}) cannot exceed remaining pending amount (₹{remaining_due})."
            })

        payment = Payment.objects.create(
            invoice=invoice,
            amount=amount,
            method=method,
            notes=notes,
        )
        invoice.update_payment_status()

        return Response(InvoiceDetailSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def dashboard_stats(self, request):
        """
        Dashboard calculations:
        - Total Sales: sum of invoices created in the period
        - Cash Received: cash payments actually collected in the period
        - Online Received: online payments actually collected in the period
        - Total Received: cash + online
        - Total Pending: sum of outstanding amounts across all invoices
        """
        date_param = request.query_params.get('date', timezone.now().date().isoformat())

        # Invoices created on selected date
        invoices_today = Invoice.objects.filter(created_at__date=date_param)
        total_sales = invoices_today.aggregate(s=Sum('grand_total'))['s'] or Decimal('0.00')
        invoices_count = invoices_today.count()

        # Payments actually received on selected date (regardless of invoice creation date)
        payments_today = Payment.objects.filter(received_at__date=date_param)
        cash_received = payments_today.filter(method='cash').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        online_received = payments_today.filter(method='online').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        total_received = cash_received + online_received

        # All-time pending across system
        all_invoices = Invoice.objects.all()
        total_billed = all_invoices.aggregate(s=Sum('grand_total'))['s'] or Decimal('0.00')
        total_collected_all_time = Payment.objects.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        total_pending = max(Decimal('0.00'), total_billed - total_collected_all_time)

        return Response({
            'date': date_param,
            'total_sales': str(total_sales),
            'invoices_count': invoices_count,
            'cash_received': str(cash_received),
            'online_received': str(online_received),
            'total_received': str(total_received),
            'total_pending': str(total_pending),
        })
