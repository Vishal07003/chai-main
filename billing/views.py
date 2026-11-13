from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied
from django.db.models import Q, Sum, Count
from django.utils import timezone
from decimal import Decimal
from django.http import HttpResponse

from .models import (
    Company,
    CompanyProductPrice,
    Customer,
    Invoice,
    InvoiceItem,
    Payment,
    PaymentAllocation,
    LedgerAdjustment,
)
from .serializers import (
    CompanySerializer,
    CompanyProductPriceSerializer,
    CustomerSerializer,
    InvoiceDetailSerializer,
    InvoiceCreateSerializer,
    PaymentSerializer,
    LedgerAdjustmentSerializer,
)
from .pdf_generator import generate_invoice_pdf
from .ledger_service import LedgerService
from .payment_service import PaymentAllocationService
from accounts.permissions import IsOwnerUser

class CompanyViewSet(viewsets.ModelViewSet):
    """
    B2B Company Account Management:
    - Search companies by name, phone, gstin
    - View contracted pricing slabs
    - Generate dynamic monthly passbook statement (Pure Query)
    - Collect payments with FIFO invoice allocation and advance tracking
    """
    queryset = Company.objects.all().order_by('name')
    serializer_class = CompanySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset()
        search = self.request.query_params.get('search', '').strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) |
                Q(trade_name__icontains=search) |
                Q(phone__icontains=search) |
                Q(gstin__icontains=search)
            )
        return queryset

    @action(detail=True, methods=['get'])
    def invoices(self, request, pk=None):
        company = self.get_object()
        invoices = company.invoices.exclude(status='void').prefetch_related('items', 'payments', 'allocations').order_by('-created_at')
        return Response(InvoiceDetailSerializer(invoices, many=True).data)

    @action(detail=True, methods=['get'])
    def ledger(self, request, pk=None):
        """
        Dynamically generates chronological financial passbook statement for the company.
        Params: ?year=YYYY&month=M (defaults to current month)
        Formula:
          Closing Balance = Opening Balance + Invoices + Debit Adjustments - Payments - Credit Adjustments
        """
        company = self.get_object()
        now = timezone.now()
        year = int(request.query_params.get('year', now.year))
        month = int(request.query_params.get('month', now.month))

        statement = LedgerService.get_monthly_statement(company=company, year=year, month=month)
        return Response(statement)

    @action(detail=True, methods=['post'], url_path='collect-payment')
    def collect_payment(self, request, pk=None):
        """
        Collects payment against company's invoices with FIFO allocation:
        Body: { "amount": 5000, "method": "online" | "cash" | "upi" | "bank_transfer", "reference_number": "...", "notes": "..." }
        - Allocates sequentially to oldest unpaid/partially paid invoices.
        - Excess amount remains as unallocated company advance credit.
        """
        company = self.get_object()
        user = request.user
        amount_val = request.data.get('amount')
        method = request.data.get('method', 'cash').lower()
        ref_no = request.data.get('reference_number', '')
        notes = request.data.get('notes', f"Collection for {company.name}")

        try:
            amount = Decimal(str(amount_val))
        except (ValueError, TypeError):
            raise ValidationError({'amount': 'Valid payment amount is required.'})

        if amount <= Decimal('0.00'):
            raise ValidationError({'amount': 'Payment amount must be greater than zero.'})

        # 1. Create top-level Payment record linked to the Company
        payment = Payment.objects.create(
            company=company,
            amount=amount,
            method=method,
            reference_number=ref_no,
            notes=notes,
            recorded_by=user,
        )

        # 2. Run FIFO allocation across unpaid company invoices
        alloc_res = PaymentAllocationService.allocate_payment_fifo(payment=payment, company=company)

        return Response({
            'success': True,
            'payment_id': payment.id,
            'amount': str(amount),
            'allocated_to_invoices': alloc_res['allocated_now'],
            'unallocated_advance_credit': alloc_res['unallocated_advance'],
            'invoices_settled': alloc_res['invoices_settled'],
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='adjustments')
    def add_adjustment(self, request, pk=None):
        """
        Adds authorized credit or debit memo to the company ledger.
        Only Owner can approve adjustments.
        Body: { "adjustment_type": "CREDIT" | "DEBIT", "amount": 500, "reason": "..." }
        """
        company = self.get_object()
        user = request.user
        if not (user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')):
            raise PermissionDenied("Only owners can create ledger adjustments.")

        adj_type = request.data.get('adjustment_type', '').upper()
        if adj_type not in ('CREDIT', 'DEBIT'):
            raise ValidationError({'adjustment_type': "Must be 'CREDIT' or 'DEBIT'."})

        amount_val = request.data.get('amount')
        reason = request.data.get('reason', '')
        if not reason:
            raise ValidationError({'reason': "Reason is required for audit history."})

        try:
            amount = Decimal(str(amount_val))
        except (ValueError, TypeError):
            raise ValidationError({'amount': 'Valid amount is required.'})

        if amount <= Decimal('0.00'):
            raise ValidationError({'amount': 'Amount must be greater than zero.'})

        adjustment = LedgerAdjustment.objects.create(
            company=company,
            adjustment_type=adj_type,
            amount=amount,
            reason=reason,
            approved_by=user,
        )

        return Response(LedgerAdjustmentSerializer(adjustment).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get', 'post'], url_path='prices')
    def prices(self, request, pk=None):
        """
        Manage contracted prices and volume slabs for this company:
        - GET: list all contracted price slabs (active and expired)
        - POST: add new contracted rate (Owner only)
          Body: { "product": 1, "custom_price": 85.00, "min_quantity": 20, "valid_from": "2026-11-01", "valid_to": null }
        """
        company = self.get_object()

        if request.method == 'GET':
            prices = company.contracted_prices.select_related('product').all().order_by('product', '-min_quantity')
            return Response(CompanyProductPriceSerializer(prices, many=True).data)

        # POST: Create contracted price
        user = request.user
        if not (user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')):
            raise PermissionDenied("Only owners can set contracted company rates.")

        serializer = CompanyProductPriceSerializer(data={**request.data, 'company': company.id})
        serializer.is_valid(raise_exception=True)
        price_obj = serializer.save(company=company)
        return Response(CompanyProductPriceSerializer(price_obj).data, status=status.HTTP_201_CREATED)


class CustomerViewSet(viewsets.ModelViewSet):
    """
    Walk-in and retail customer management for stall owners:
    - Search by name or phone
    - List customer records with aggregated bills and totals
    - View customer invoices & collect direct payments
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
        invoices = customer.invoices.all().prefetch_related('items', 'payments', 'allocations')

        date_param = request.query_params.get('date')
        if date_param:
            invoices = invoices.filter(created_at__date=date_param)

        serializer = InvoiceDetailSerializer(invoices, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def summary(self, request, pk=None):
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
        customer = self.get_object()
        amount_val = request.data.get('amount') or request.data.get('amountToDeduct')
        method = request.data.get('method', 'cash').lower()
        notes = request.data.get('notes', f"Customer collection for {customer.name}")

        try:
            amount = Decimal(str(amount_val))
        except (ValueError, TypeError):
            raise ValidationError({'amount': 'Valid payment amount is required.'})

        if amount <= Decimal('0.00'):
            raise ValidationError({'amount': 'Payment amount must be greater than zero.'})

        pending_invoices = customer.invoices.exclude(status='paid').order_by('created_at')
        if not pending_invoices.exists():
            raise ValidationError({'error': 'Customer has no outstanding balance to pay.'})

        # Create master payment and allocate FIFO
        pm = Payment.objects.create(
            amount=amount,
            method=method if method in ('cash', 'online', 'upi', 'bank_transfer') else 'cash',
            notes=notes,
            recorded_by=request.user,
        )
        alloc_res = PaymentAllocationService.allocate_payment_fifo(
            payment=pm,
            target_invoices=list(pending_invoices)
        )

        return Response({
            'success': True,
            'customer_id': customer.id,
            'amount_requested': str(amount),
            'amount_allocated': alloc_res['allocated_now'],
            'unallocated_credit': alloc_res['unallocated_advance'],
            'invoices_settled': alloc_res['invoices_settled'],
        }, status=status.HTTP_201_CREATED)


class InvoiceViewSet(viewsets.ModelViewSet):
    """
    Quick Billing and Invoice Management:
    - Standalone Quick Billing for walk-ins
    - View invoice details with items, total_paid, pending_amount, payments
    - Add subsequent payment to invoice
    - Filter invoices by date, company, customer, payment status
    - Dashboard metrics aggregation
    """
    queryset = Invoice.objects.select_related('customer', 'company', 'order').prefetch_related('items', 'payments', 'allocations').all().order_by('-created_at')
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
        company_id = self.request.query_params.get('company')
        search = self.request.query_params.get('search', '').strip()

        if status_param:
            queryset = queryset.filter(status=status_param)
        if date_param:
            queryset = queryset.filter(created_at__date=date_param)
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        if company_id:
            queryset = queryset.filter(company_id=company_id)
        if search:
            queryset = queryset.filter(
                Q(invoice_number__icontains=search) |
                Q(customer_name__icontains=search) |
                Q(customer_phone__icontains=search) |
                Q(company__name__icontains=search)
            )

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = serializer.save()
        return Response(InvoiceDetailSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def payments(self, request, pk=None):
        """
        Record a direct payment towards this specific invoice
        """
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
            company=invoice.company,
            invoice=invoice,
            amount=amount,
            method=method,
            notes=notes,
            recorded_by=request.user,
        )
        PaymentAllocation.objects.create(
            payment=payment,
            invoice=invoice,
            allocated_amount=amount,
            notes=f"Direct payment to {invoice.invoice_number}"
        )
        invoice.update_payment_status()

        return Response(InvoiceDetailSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def pdf(self, request, pk=None):
        invoice = self.get_object()
        pdf_bytes = generate_invoice_pdf(invoice)

        filename = f"ChaiFlow_{invoice.invoice_number or f'INV-{invoice.id}'}.pdf"
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        response['Content-Length'] = len(pdf_bytes)
        return response

    @action(detail=False, methods=['get'])
    def dashboard_stats(self, request):
        date_param = request.query_params.get('date', timezone.now().date().isoformat())

        invoices_today = Invoice.objects.filter(created_at__date=date_param).exclude(status='void')
        total_sales = invoices_today.aggregate(s=Sum('grand_total'))['s'] or Decimal('0.00')
        invoices_count = invoices_today.count()

        payments_today = Payment.objects.filter(received_at__date=date_param)
        cash_received = payments_today.filter(method='cash').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        online_received = payments_today.exclude(method='cash').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        total_received = cash_received + online_received

        all_invoices = Invoice.objects.exclude(status='void')
        total_billed = all_invoices.aggregate(s=Sum('grand_total'))['s'] or Decimal('0.00')
        all_payments = Payment.objects.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        total_pending = max(Decimal('0.00'), total_billed - all_payments)

        return Response({
            'date': date_param,
            'today_sales': str(total_sales),
            'total_sales': str(total_sales),
            'today_received': str(total_received),
            'total_received': str(total_received),
            'cash_received': str(cash_received),
            'online_received': str(online_received),
            'invoices_count': invoices_count,
            'total_pending': str(total_pending),
        })
