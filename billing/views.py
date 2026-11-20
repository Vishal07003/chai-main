from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.renderers import BaseRenderer
from rest_framework.exceptions import ValidationError, PermissionDenied
from django.db.models import Q, Sum, Count
from django.utils import timezone
from decimal import Decimal
from django.http import HttpResponse

class PassthroughPdfRenderer(BaseRenderer):
    """
    Renders raw binary PDF data for DRF content negotiation.
    Matches Accept: application/pdf, Accept: */*, or default requests.
    """
    media_type = 'application/pdf'
    format = 'pdf'
    charset = None
    render_style = 'binary'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data

from .models import (
    Company,
    CompanyProductPrice,
    Customer,
    Invoice,
    InvoiceItem,
    Payment,
    PaymentAllocation,
    LedgerAdjustment,
    InvoiceOrder,
    DraftInvoice,
    DraftInvoiceItem,
    DraftInvoiceOrder,
)
from .serializers import (
    CompanySerializer,
    CompanyProductPriceSerializer,
    CustomerSerializer,
    InvoiceDetailSerializer,
    InvoiceCreateSerializer,
    PaymentSerializer,
    LedgerAdjustmentSerializer,
    DraftInvoiceDetailSerializer,
    DraftInvoiceCreateSerializer,
    DraftInvoiceUpdateSerializer,
    DraftFinalizeSerializer,
)
from .pdf_generator import generate_invoice_pdf
from .ledger_service import LedgerService
from .payment_service import PaymentAllocationService
from .draft_service import DraftBillingService
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

        # 1. Create top-level Payment record linked to the Company using centralized service
        payment = PaymentAllocationService.record_payment(
            amount=amount,
            method=method,
            company=company,
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
        invoices = customer.invoices.exclude(status='void')
        total_billed = invoices.aggregate(s=Sum('grand_total'))['s'] or Decimal('0.00')
        total_paid = customer.payments.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        pending = sum(inv.pending_amount for inv in invoices)
        last_invoice = invoices.order_by('-created_at').first()

        return Response({
            'customer_id': customer.id,
            'name': customer.name,
            'phone': customer.phone,
            'address': customer.address,
            'total_invoices': invoices.count(),
            'total_orders': customer.customer_orders.count(),
            'total_billed': str(total_billed),
            'total_paid': str(total_paid),
            'total_pending': str(pending),
            'last_purchase_date': last_invoice.created_at.isoformat() if last_invoice else None,
        })

    @action(detail=True, methods=['get'])
    def ledger(self, request, pk=None):
        """
        Dynamically generates chronological financial passbook statement for the customer.
        Params: ?year=YYYY&month=M (defaults to current month)
        """
        customer = self.get_object()
        now = timezone.now()
        year = int(request.query_params.get('year', now.year))
        month = int(request.query_params.get('month', now.month))

        statement = LedgerService.get_customer_statement(customer=customer, year=year, month=month)
        return Response(statement)

    @action(detail=True, methods=['get'])
    def orders(self, request, pk=None):
        """List all customer orders"""
        customer = self.get_object()
        from orders.serializers import OrderSerializer
        orders = customer.customer_orders.all().order_by('-created_at')
        return Response(OrderSerializer(orders, many=True).data)

    @action(detail=True, methods=['get', 'post'], url_path='prices')
    def prices(self, request, pk=None):
        """
        Manage customer-specific selling prices (Owner only):
        - GET: list all customer custom prices
        - POST: add or update custom rate for a product
          Body: { "product": 1, "custom_price": 85.00 }
        """
        customer = self.get_object()
        from .models import CustomerProductPrice
        from .serializers import CustomerProductPriceSerializer

        if request.method == 'GET':
            prices = customer.custom_prices.filter(is_active=True).select_related('product')
            return Response(CustomerProductPriceSerializer(prices, many=True).data)

        # POST
        user = request.user
        if not (user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')):
            raise PermissionDenied("Only owners can set custom customer rates.")

        product_id = request.data.get('product')
        custom_price = request.data.get('custom_price')
        if not product_id or custom_price is None:
            raise ValidationError({'detail': 'product and custom_price are required.'})

        price_obj, _ = CustomerProductPrice.objects.update_or_create(
            customer=customer,
            product_id=product_id,
            defaults={
                'custom_price': Decimal(str(custom_price)),
                'is_active': True,
            }
        )
        return Response(CustomerProductPriceSerializer(price_obj).data, status=status.HTTP_201_CREATED)

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

        # Create payment linked directly to Customer using centralized service
        pm = PaymentAllocationService.record_payment(
            amount=amount,
            method=method,
            customer=customer,
            company=customer.company,
            notes=notes,
            recorded_by=request.user,
        )

        alloc_res = PaymentAllocationService.allocate_payment_fifo(
            payment=pm,
            customer=customer,
        )
        allocated_now = Decimal(str(alloc_res['allocated_now']))
        invoices_settled = alloc_res['invoices_settled']

        unallocated_advance = max(Decimal('0.00'), amount - allocated_now)

        return Response({
            'success': True,
            'customer_id': customer.id,
            'amount_requested': str(amount),
            'amount_allocated': str(allocated_now),
            'unallocated_credit': str(unallocated_advance),
            'invoices_settled': invoices_settled,
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
        ref_no = request.data.get('reference_number', '')

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

        # Record and allocate using centralized payment service
        payment = PaymentAllocationService.record_payment(
            amount=amount,
            method=method,
            customer=invoice.customer,
            company=invoice.company,
            invoice=invoice,
            reference_number=ref_no,
            notes=notes,
            recorded_by=request.user,
        )
        PaymentAllocationService.allocate_payment_fifo(
            payment=payment,
            target_invoices=[invoice]
        )
        # Fetch fresh invoice instance without stale prefetch caches
        fresh_invoice = self.get_queryset().get(id=invoice.id)

        return Response(InvoiceDetailSerializer(fresh_invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], renderer_classes=[PassthroughPdfRenderer])
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
        total_pending = sum(inv.pending_amount for inv in all_invoices)
        
        # Payment breakdown counts
        paid_invoices_count = all_invoices.filter(status='paid').count()
        partially_paid_invoices_count = all_invoices.filter(status='partially_paid').count()
        unpaid_invoices_count = all_invoices.filter(status='unpaid').count()
        
        # Defaulters: count of customers with positive pending balance and an explicitly assigned past due date
        today = timezone.now().date()
        def is_customer_defaulter(c):
            for inv in c.invoices.exclude(status='void'):
                if inv.pending_amount > Decimal('0.00') and inv.due_date is not None and inv.due_date < today:
                    return True
            return False

        defaulters_count = sum(1 for c in Customer.objects.all() if is_customer_defaulter(c))

        # Orders metrics
        from orders.models import Order
        today_orders_count = Order.objects.filter(created_at__date=date_param).count()
        pending_orders_count = Order.objects.filter(status='pending').count()

        return Response({
            'date': date_param,
            'today_sales': str(total_sales),
            'total_sales': str(total_sales),
            'today_received': str(total_received),
            'total_received': str(total_received),
            'cash_received': str(cash_received),
            'online_received': str(online_received),
            'invoices_count': invoices_count,
            'today_orders_count': today_orders_count,
            'pending_orders_count': pending_orders_count,
            'total_pending': str(total_pending),
            'paid_invoices_count': paid_invoices_count,
            'partially_paid_invoices_count': partially_paid_invoices_count,
            'unpaid_invoices_count': unpaid_invoices_count,
            'defaulters_count': defaulters_count,
        })


class DraftInvoiceViewSet(viewsets.ModelViewSet):
    """
    Phase 2: Centralized Draft Invoice API:
    - POST /api/v1/billing/drafts/: Create new draft for Quick Billing, Single Order, Consolidated Orders, or Recurring.
    - GET /api/v1/billing/drafts/: List drafts with customer, status, and billing_source filters.
    - GET /api/v1/billing/drafts/{id}/: Retrieve draft detail with items and reserved orders.
    - PATCH /api/v1/billing/drafts/{id}/: Edit draft items, rates, quantities, discounts, due_date, or order selections.
    - POST /api/v1/billing/drafts/{id}/cancel/: Cancel draft and release order reservations.
    - POST /api/v1/billing/drafts/{id}/finalize/: Atomically finalize draft into official Invoice.
    """
    queryset = DraftInvoice.objects.select_related('customer', 'created_by', 'official_invoice').prefetch_related('items', 'draft_orders__order').all().order_by('-created_at')
    permission_classes = [permissions.IsAuthenticated, IsOwnerUser]

    def get_serializer_class(self):
        if self.action == 'create':
            return DraftInvoiceCreateSerializer
        if self.action in ('update', 'partial_update'):
            return DraftInvoiceUpdateSerializer
        if self.action == 'finalize':
            return DraftFinalizeSerializer
        return DraftInvoiceDetailSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        status_param = self.request.query_params.get('status')
        source_param = self.request.query_params.get('billing_source')
        customer_id = self.request.query_params.get('customer')
        search = self.request.query_params.get('search', '').strip()

        if status_param:
            queryset = queryset.filter(status=status_param)
        if source_param:
            queryset = queryset.filter(billing_source=source_param)
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        if search:
            queryset = queryset.filter(
                Q(draft_number__icontains=search) |
                Q(customer_name__icontains=search) |
                Q(customer_phone__icontains=search)
            )

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        customer_id = data.get('customer_id')
        customer = Customer.objects.filter(id=customer_id).first() if customer_id else None

        draft = DraftBillingService.create_draft(
            customer=customer,
            billing_source=data.get('billing_source', 'quick_billing'),
            items_data=data.get('items'),
            order_ids=data.get('order_ids'),
            discount_amount=data.get('discount_amount', Decimal('0.00')),
            due_date=data.get('due_date'),
            notes=data.get('notes', ''),
            created_by=request.user,
        )

        return Response(DraftInvoiceDetailSerializer(draft).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        draft = self.get_object()
        serializer = self.get_serializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        customer_id = data.get('customer_id')
        customer = Customer.objects.filter(id=customer_id).first() if customer_id is not None else None

        updated_draft = DraftBillingService.update_draft(
            draft=draft,
            customer=customer if customer_id is not None else None,
            items_data=data.get('items'),
            order_ids=data.get('order_ids'),
            discount_amount=data.get('discount_amount'),
            due_date=data.get('due_date'),
            notes=data.get('notes'),
        )

        return Response(DraftInvoiceDetailSerializer(updated_draft).data, status=status.HTTP_200_OK)

    def update(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        draft = self.get_object()
        if draft.status == 'finalized':
            raise ValidationError({'detail': 'Cannot cancel a finalized draft.'})
        cancelled_draft = DraftBillingService.cancel_draft(draft)
        return Response(DraftInvoiceDetailSerializer(cancelled_draft).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def finalize(self, request, pk=None):
        draft = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        notes = serializer.validated_data.get('notes')

        official_invoice = DraftBillingService.finalize_draft(
            draft_id=draft.id,
            owner_user=request.user,
            notes=notes,
        )

        return Response({
            'success': True,
            'message': f"Draft #{draft.draft_number} successfully finalized into official Invoice #{official_invoice.invoice_number}.",
            'invoice': InvoiceDetailSerializer(official_invoice).data,
            'draft': DraftInvoiceDetailSerializer(draft).data,
        }, status=status.HTTP_201_CREATED)

