from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied
from django.db.models import Q, Sum, Count
from django.utils import timezone
from decimal import Decimal

from .models import Customer, Invoice, InvoiceItem, Payment
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

    @action(detail=True, methods=['get'])
    def summary(self, request, pk=None):
        """
        Customer purchase summary:
        - Total billed
        - Total paid
        - Total pending
        - Invoice count
        - Last purchase date
        """
        customer = self.get_object()
        invoices = customer.invoices.all()
        total_billed = invoices.aggregate(s=Sum('grand_total'))['s'] or Decimal('0.00')
        total_paid = Payment.objects.filter(invoice__customer=customer).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        pending = max(Decimal('0.00'), total_billed - total_paid)
        last_invoice = invoices.order_by('-created_at').first()

        return Response({
            'customer_id': customer.id,
            'name': customer.name,
            'phone': customer.phone,
            'total_invoices': invoices.count(),
            'total_billed': str(total_billed),
            'total_paid': str(total_paid),
            'total_pending': str(pending),
            'last_purchase_date': last_invoice.created_at.isoformat() if last_invoice else None,
        })

    @action(detail=True, methods=['post'], url_path='collect-payment')
    def collect_payment(self, request, pk=None):
        """
        Collect payment towards customer's oldest pending invoices.
        Body: { "amount": 500, "method": "cash" | "online", "notes": "..." }
        """
        customer = self.get_object()
        amount_val = request.data.get('amount') or request.data.get('amountToDeduct')
        method = request.data.get('method', 'cash').lower()
        notes = request.data.get('notes', f"Customer direct collection for {customer.name}")

        try:
            amount = Decimal(str(amount_val))
        except (ValueError, TypeError):
            raise ValidationError({'amount': 'Valid payment amount is required.'})

        if amount <= Decimal('0.00'):
            raise ValidationError({'amount': 'Payment amount must be greater than zero.'})

        # Find unpaid or partially paid invoices ordered from oldest to newest
        pending_invoices = customer.invoices.exclude(status='paid').order_by('created_at')
        if not pending_invoices.exists():
            raise ValidationError({'error': 'Customer has no outstanding balance to pay.'})

        remaining_to_allocate = amount
        payments_created = []

        for inv in pending_invoices:
            due = inv.pending_amount
            if due <= Decimal('0.00'):
                continue

            pay_amount = min(remaining_to_allocate, due)
            pm = Payment.objects.create(
                invoice=inv,
                amount=pay_amount,
                method=method if method in ('cash', 'online') else 'cash',
                notes=notes,
            )
            inv.update_payment_status()
            payments_created.append(pm.id)

            remaining_to_allocate -= pay_amount
            if remaining_to_allocate <= Decimal('0.00'):
                break

        allocated = amount - remaining_to_allocate
        return Response({
            'success': True,
            'customer_id': customer.id,
            'amount_requested': str(amount),
            'amount_allocated': str(allocated),
            'unallocated_credit': str(remaining_to_allocate),
            'payments_created': len(payments_created),
        }, status=status.HTTP_201_CREATED)

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

        # Period breakdown (Product-wise and Customer-wise for ReportsScreen)
        product_sales = (
            InvoiceItem.objects.filter(invoice__created_at__date=date_param)
            .values('product_name')
            .annotate(
                units_sold=Sum('quantity'),
                revenue=Sum('total_price'),
            )
            .order_by('-revenue')[:10]
        )

        customer_sales = (
            invoices_today.values('customer_name')
            .annotate(
                bills_count=Count('id'),
                total_spent=Sum('grand_total'),
            )
            .order_by('-total_spent')[:10]
        )

        return Response({
            'date': date_param,
            'total_sales': str(total_sales),
            'invoices_count': invoices_count,
            'cash_received': str(cash_received),
            'online_received': str(online_received),
            'total_received': str(total_received),
            'total_pending': str(total_pending),
            'product_sales': [
                {
                    'product_name': p['product_name'],
                    'units_sold': p['units_sold'],
                    'revenue': str(p['revenue']),
                }
                for p in product_sales
            ],
            'customer_sales': [
                {
                    'customer_name': c['customer_name'],
                    'bills_count': c['bills_count'],
                    'total_spent': str(c['total_spent']),
                }
                for c in customer_sales
            ],
        })
