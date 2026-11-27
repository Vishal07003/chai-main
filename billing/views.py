import logging
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.renderers import BaseRenderer
from rest_framework.exceptions import ValidationError, PermissionDenied
from django.db import models, transaction
from django.db.models import Q, Sum, Count
from django.utils import timezone
from decimal import Decimal
from django.http import HttpResponse

logger = logging.getLogger(__name__)

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


class PassthroughExcelRenderer(BaseRenderer):
    """
    Renders raw binary XLSX data for DRF content negotiation.
    Matches Accept: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,
    Accept: */*, or custom binary requests without triggering 406 Not Acceptable.
    """
    media_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    format = 'xlsx'
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
    Expense,
    InvoiceAuditLog,
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
    ExpenseSerializer,
)
from .pdf_generator import generate_invoice_pdf, generate_payment_receipt_pdf
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

        # Notify Owner of payment received
        try:
            from notifications.services import NotificationService
            NotificationService.on_payment_received(payment, customer_name=company.name)
        except Exception as e:
            logger.warning(f"Failed to dispatch payment received notification: {e}")

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

    @action(detail=True, methods=['get'], url_path='ledger-pdf', renderer_classes=[PassthroughPdfRenderer])
    def ledger_pdf(self, request, pk=None):
        """
        Exports authenticated Customer Ledger Statement as A4 PDF.
        Params: ?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
        """
        customer = self.get_object()
        from .customer_credit_service import CustomerCreditService
        from .customer_ledger_pdf import generate_customer_ledger_pdf

        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')

        overview = LedgerService.get_customer_monthly_ledger_overview(customer=customer)
        balances = CustomerCreditService.get_balances(customer=customer)

        invoices = overview.get('monthly_records', [])
        payments = overview.get('payment_history', [])

        if start_date_str and end_date_str:
            invoices = [inv for inv in invoices if inv.get('created_at') and start_date_str <= inv['created_at'][:10] <= end_date_str]
            payments = [p for p in payments if p.get('received_at') and start_date_str <= p['received_at'][:10] <= end_date_str]
            period_label = f"{start_date_str} to {end_date_str}"
        else:
            period_label = "Complete Ledger History"

        total_invoiced = sum(Decimal(str(inv.get('total_bill', 0))) for inv in invoices)
        total_collections = sum(Decimal(str(p.get('amount', 0))) for p in payments if not p.get('is_reversed'))
        outstanding = sum(Decimal(str(inv.get('remaining_balance', 0))) for inv in invoices)

        period_data = {
            'period_label': period_label,
            'opening_balance': '0.00',
            'total_invoiced': str(total_invoiced),
            'total_collections': str(total_collections),
            'outstanding_balance': str(outstanding),
            'available_credit': str(balances['available_credit']),
            'refund_due': str(balances['refund_due']),
            'invoices': invoices,
            'payments': payments,
        }

        pdf_bytes = generate_customer_ledger_pdf(customer, period_data)
        filename = f"ChaiFlow_Ledger_{customer.name.replace(' ', '_')}_{customer.id}.pdf"

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        response['Content-Length'] = len(pdf_bytes)
        return response

    @action(detail=True, methods=['get'], url_path='ledger-excel', renderer_classes=[PassthroughExcelRenderer])
    def ledger_excel(self, request, pk=None):
        """
        Exports authenticated Customer Ledger Statement as XLSX Excel file.
        Params: ?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
        """
        customer = self.get_object()
        from .customer_credit_service import CustomerCreditService
        from .customer_ledger_excel import generate_customer_ledger_excel

        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')

        overview = LedgerService.get_customer_monthly_ledger_overview(customer=customer)
        balances = CustomerCreditService.get_balances(customer=customer)

        invoices = overview.get('monthly_records', [])
        payments = overview.get('payment_history', [])

        if start_date_str and end_date_str:
            invoices = [inv for inv in invoices if inv.get('created_at') and start_date_str <= inv['created_at'][:10] <= end_date_str]
            payments = [p for p in payments if p.get('received_at') and start_date_str <= p['received_at'][:10] <= end_date_str]
            period_label = f"{start_date_str} to {end_date_str}"
        else:
            period_label = "Complete Ledger History"

        total_invoiced = sum(Decimal(str(inv.get('total_bill', 0))) for inv in invoices)
        total_collections = sum(Decimal(str(p.get('amount', 0))) for p in payments if not p.get('is_reversed'))
        outstanding = sum(Decimal(str(inv.get('remaining_balance', 0))) for inv in invoices)

        period_data = {
            'period_label': period_label,
            'opening_balance': '0.00',
            'total_invoiced': str(total_invoiced),
            'total_collections': str(total_collections),
            'outstanding_balance': str(outstanding),
            'available_credit': str(balances['available_credit']),
            'refund_due': str(balances['refund_due']),
            'invoices': invoices,
            'payments': payments,
        }

        excel_bytes = generate_customer_ledger_excel(customer, period_data)
        filename = f"ChaiFlow_Ledger_{customer.name.replace(' ', '_')}_{customer.id}.xlsx"

        response = HttpResponse(
            excel_bytes,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        response['Content-Length'] = len(excel_bytes)
        return response

    @action(detail=True, methods=['get'], url_path='monthly-ledger')
    def monthly_ledger(self, request, pk=None):
        """
        Requirements 6: Customer-wise comprehensive monthly records,
        all past unpaid invoices, cash collection history and allocation details.
        """
        customer = self.get_object()
        overview = LedgerService.get_customer_monthly_ledger_overview(customer=customer)
        return Response(overview)

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

        # Notify Owner of payment received
        try:
            from notifications.services import NotificationService
            NotificationService.on_payment_received(pm, customer_name=customer.name)
        except Exception as e:
            logger.warning(f"Failed to dispatch customer payment received notification: {e}")

        return Response({
            'success': True,
            'customer_id': customer.id,
            'amount_requested': str(amount),
            'amount_allocated': str(allocated_now),
            'unallocated_credit': str(unallocated_advance),
            'invoices_settled': invoices_settled,
            'settled_invoice_ids': alloc_res.get('settled_invoices', []),
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
    queryset = Invoice.objects.select_related('customer', 'company', 'order').prefetch_related('items', 'payments', 'allocations', 'audit_logs').all().order_by('-created_at')
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
        billing_type = self.request.query_params.get('billing_type')
        billing_period = self.request.query_params.get('billing_period')
        search = self.request.query_params.get('search', '').strip()

        if status_param:
            queryset = queryset.filter(status=status_param)
        if date_param:
            queryset = queryset.filter(created_at__date=date_param)
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        if company_id:
            queryset = queryset.filter(company_id=company_id)
        if billing_type:
            queryset = queryset.filter(billing_type=billing_type)
        if billing_period:
            queryset = queryset.filter(billing_period=billing_period)
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

        # Notify Owner of invoice generation
        try:
            from notifications.services import NotificationService
            NotificationService.on_invoice_generated(invoice)
        except Exception as e:
            logger.warning(f"Failed to dispatch invoice generated notification: {e}")

        return Response(InvoiceDetailSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch', 'put'])
    def edit_bill(self, request, pk=None):
        """
        Part 4: Owner edit saved bill with audit history and ledger synchronization.
        Editable fields:
        - items: [{'product_id': int, 'product_name': str, 'unit_price': str/num, 'quantity': int}]
        - discount_amount: Decimal
        - notes: str
        - due_date: date
        - reason: audit explanation
        """
        user = request.user
        if not (user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')):
            raise PermissionDenied("Only owners can edit saved bills.")

        invoice = self.get_object()
        data = request.data

        # Enforce mandatory correction reason
        reason = data.get('reason')
        if not reason or not str(reason).strip():
            raise ValidationError({'reason': 'A mandatory correction reason must be provided.'})

        with transaction.atomic():
            locked_invoice = Invoice.objects.select_for_update().get(id=invoice.id)

            # 1. Snapshot previous state
            previous_items_snap = [
                {
                    'id': it.id,
                    'product_id': it.product_id,
                    'product_name': it.product_name,
                    'unit_price': str(it.unit_price),
                    'quantity': it.quantity,
                    'total_price': str(it.total_price),
                }
                for it in locked_invoice.items.all()
            ]
            prev_subtotal = locked_invoice.subtotal
            prev_discount = locked_invoice.discount_amount
            prev_grand_total = locked_invoice.grand_total
            prev_notes = locked_invoice.notes

            # 2. Update items if provided
            items_payload = data.get('items')
            if items_payload is not None:
                if not isinstance(items_payload, list) or len(items_payload) == 0:
                    raise ValidationError({'items': 'A bill must have at least one product line.'})

                locked_invoice.items.all().delete()
                subtotal = Decimal('0.00')

                from products.models import Product
                p_ids = [it.get('product_id') for it in items_payload if it.get('product_id')]
                prod_map = {p.id: p for p in Product.objects.filter(id__in=p_ids)}

                for it in items_payload:
                    pid = it.get('product_id')
                    p_obj = prod_map.get(pid) if pid else None
                    p_name = it.get('product_name') or (p_obj.name if p_obj else 'Item')
                    qty = int(it.get('quantity', 1))
                    if qty <= 0:
                        raise ValidationError({'items': 'Product quantities must be positive.'})

                    if 'unit_price' in it:
                        u_price = Decimal(str(it['unit_price']))
                    elif p_obj:
                        u_price = p_obj.price
                    else:
                        u_price = Decimal('0.00')

                    line_total = u_price * qty
                    subtotal += line_total

                    unit_cost_val = getattr(p_obj, 'cost_price', None) if p_obj else None
                    total_cost_val = (unit_cost_val * qty) if unit_cost_val is not None else None

                    InvoiceItem.objects.create(
                        invoice=locked_invoice,
                        product=p_obj,
                        product_name=p_name,
                        unit_price=u_price,
                        quantity=qty,
                        total_price=line_total,
                        unit_cost=unit_cost_val,
                        total_cost=total_cost_val,
                    )
                locked_invoice.subtotal = subtotal
            else:
                subtotal = locked_invoice.subtotal

            # 3. Update discount
            if 'discount_amount' in data:
                disc_val = Decimal(str(data['discount_amount']))
                if disc_val < Decimal('0.00'):
                    raise ValidationError({'discount_amount': 'Discount amount cannot be negative.'})
                locked_invoice.discount_amount = disc_val

            # 4. Update notes & due_date
            if 'notes' in data:
                locked_invoice.notes = data['notes']
            if 'due_date' in data:
                locked_invoice.due_date = data['due_date']

            # 5. Compute new grand total & update payment status
            new_grand_total = max(Decimal('0.00'), locked_invoice.subtotal - locked_invoice.discount_amount)
            locked_invoice.grand_total = new_grand_total
            locked_invoice.save()
            locked_invoice.update_payment_status()

            # 6. Record Audit Log
            updated_items_snap = [
                {
                    'id': it.id,
                    'product_id': it.product_id,
                    'product_name': it.product_name,
                    'unit_price': str(it.unit_price),
                    'quantity': it.quantity,
                    'total_price': str(it.total_price),
                }
                for it in locked_invoice.items.all()
            ]

            InvoiceAuditLog.objects.create(
                invoice=locked_invoice,
                edited_by=user,
                previous_total=prev_grand_total,
                updated_total=new_grand_total,
                previous_values={
                    'subtotal': str(prev_subtotal),
                    'discount_amount': str(prev_discount),
                    'grand_total': str(prev_grand_total),
                    'notes': prev_notes,
                    'items': previous_items_snap,
                },
                updated_values={
                    'subtotal': str(locked_invoice.subtotal),
                    'discount_amount': str(locked_invoice.discount_amount),
                    'grand_total': str(locked_invoice.grand_total),
                    'notes': locked_invoice.notes,
                    'items': updated_items_snap,
                },
                reason=str(reason).strip(),
            )

            # 7. Phase 5: Handle excess payment treatment if corrected total is less than already received payments
            excess_treatment = data.get('excess_treatment')  # 'credit' or 'refund'
            excess_amount = max(Decimal('0.00'), locked_invoice.total_paid - new_grand_total)
            if excess_amount > Decimal('0.00') and excess_treatment:
                from .customer_credit_service import CustomerCreditService
                CustomerCreditService.record_excess_treatment(
                    invoice=locked_invoice,
                    treatment=excess_treatment,
                    amount=excess_amount,
                    recorded_by=user,
                    reason=f"Correction excess treatment: {excess_treatment.upper()} ({reason})"
                )

        fresh_inv = self.get_queryset().get(id=invoice.id)
        return Response(InvoiceDetailSerializer(fresh_inv).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='generate-monthly')
    def generate_monthly(self, request):
        """
        Generate monthly consolidated invoices for delivered orders within a calendar month.
        Body: { "year": 2026, "month": 11, "customer_ids": [1, 2] (optional) }
        """
        user = request.user
        if not (user.is_staff or user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'owner')):
            raise PermissionDenied("Only the business owner can generate monthly bills.")

        year = request.data.get('year')
        month = request.data.get('month')
        customer_ids = request.data.get('customer_ids')

        now = timezone.now()
        try:
            year = int(year) if year is not None else now.year
            month = int(month) if month is not None else now.month
        except (ValueError, TypeError):
            raise ValidationError({'detail': 'year and month must be valid integers.'})

        if month < 1 or month > 12:
            raise ValidationError({'month': 'Month must be between 1 and 12.'})

        from .monthly_service import MonthlyBillingService
        generated = MonthlyBillingService.generate_monthly_invoices_for_month(
            year=year,
            month=month,
            owner_user=user,
            customer_ids=customer_ids if isinstance(customer_ids, list) else None,
        )

        return Response({
            'success': True,
            'billing_period': f"{year:04d}-{month:02d}",
            'invoices_generated_count': len(generated),
            'invoices': InvoiceDetailSerializer(generated, many=True).data,
        }, status=status.HTTP_201_CREATED)

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

        # Notify Owner of payment received for invoice
        try:
            from notifications.services import NotificationService
            NotificationService.on_payment_received(
                payment,
                customer_name=invoice.customer_name,
                invoice_no=invoice.invoice_number
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch invoice payment received notification: {e}")

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

    @action(detail=True, methods=['get'])
    def breakdown(self, request, pk=None):
        """
        Returns structured date-wise and order-wise breakdown for a monthly invoice.
        Hierarchy: Customer -> Billing Period -> Delivery Date -> Individual Order -> Product Item.
        """
        invoice = self.get_object()
        from .monthly_service import MonthlyBillingService
        breakdown_data = MonthlyBillingService.get_monthly_invoice_breakdown(invoice)
        return Response(breakdown_data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], renderer_classes=[PassthroughExcelRenderer])
    def excel(self, request, pk=None):
        """
        Exports structured Excel (.xlsx) file for a monthly invoice with date-wise sections and daily subtotals.
        """
        invoice = self.get_object()
        from .monthly_excel_generator import generate_monthly_invoice_excel
        excel_bytes = generate_monthly_invoice_excel(invoice)

        filename = f"ChaiFlow_Monthly_{invoice.invoice_number or f'INV-{invoice.id}'}.xlsx"
        response = HttpResponse(
            excel_bytes,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        response['Content-Length'] = len(excel_bytes)
        return response

    @action(detail=False, methods=['get'])
    def dashboard_stats(self, request):
        date_param = request.query_params.get('date', timezone.now().date().isoformat())

        invoices_today = Invoice.objects.filter(created_at__date=date_param).exclude(status='void')
        total_sales = invoices_today.aggregate(s=Sum('grand_total'))['s'] or Decimal('0.00')
        invoices_count = invoices_today.count()

        payments_today = Payment.objects.filter(received_at__date=date_param, is_reversed=False)
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

        partner_id = data.get('delivery_partner_id')
        delivery_partner = None
        if partner_id:
            from deliveries.models import DeliveryPartner
            delivery_partner = DeliveryPartner.objects.filter(id=partner_id).first()

        draft = DraftBillingService.create_draft(
            customer=customer,
            billing_source=data.get('billing_source', 'quick_billing'),
            items_data=data.get('items'),
            order_ids=data.get('order_ids'),
            discount_amount=data.get('discount_amount', Decimal('0.00')),
            due_date=data.get('due_date'),
            delivery_partner=delivery_partner,
            delivery_address=data.get('delivery_address', ''),
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

        delivery_partner = None
        clear_delivery_partner = False
        if 'delivery_partner_id' in data:
            partner_id = data.get('delivery_partner_id')
            if partner_id is None:
                clear_delivery_partner = True
            else:
                from deliveries.models import DeliveryPartner
                delivery_partner = DeliveryPartner.objects.filter(id=partner_id).first()

        updated_draft = DraftBillingService.update_draft(
            draft=draft,
            customer=customer if customer_id is not None else None,
            delivery_partner=delivery_partner,
            clear_delivery_partner=clear_delivery_partner,
            delivery_address=data.get('delivery_address'),
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

        # Notify Owner of finalized invoice
        try:
            from notifications.services import NotificationService
            NotificationService.on_invoice_generated(official_invoice)
        except Exception as e:
            logger.warning(f"Failed to dispatch finalized invoice notification: {e}")

        return Response({
            'success': True,
            'message': f"Draft #{draft.draft_number} successfully finalized into official Invoice #{official_invoice.invoice_number}.",
            'invoice': InvoiceDetailSerializer(official_invoice).data,
            'draft': DraftInvoiceDetailSerializer(draft).data,
        }, status=status.HTTP_201_CREATED)


class ExpenseViewSet(viewsets.ModelViewSet):
    """
    Phase R1: Business Expense Management (Owner Only)
    - Full CRUD for operating expenses (Rent, Utilities, Transport, etc.).
    - Filtering by category, payment_method, date, month, year, search.
    - Excludes raw material inventory purchases from operating expenses.
    """
    queryset = Expense.objects.select_related('recorded_by').all().order_by('-expense_date', '-created_at')
    serializer_class = ExpenseSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwnerUser]

    def get_queryset(self):
        queryset = super().get_queryset()
        category_param = self.request.query_params.get('category')
        method_param = self.request.query_params.get('payment_method')
        date_param = self.request.query_params.get('date')
        from_date = self.request.query_params.get('from_date')
        to_date = self.request.query_params.get('to_date')
        search = self.request.query_params.get('search', '').strip()

        if category_param:
            queryset = queryset.filter(category=category_param)
        if method_param:
            queryset = queryset.filter(payment_method=method_param)
        if date_param:
            queryset = queryset.filter(expense_date=date_param)
        if from_date:
            queryset = queryset.filter(expense_date__gte=from_date)
        if to_date:
            queryset = queryset.filter(expense_date__lte=to_date)
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) |
                Q(notes__icontains=search)
            )
        return queryset

    def perform_create(self, serializer):
        serializer.save(recorded_by=self.request.user)


class PaymentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Payments ViewSet:
    - Lists and retrieves payment collections for authorized owners/staff.
    - Generates branded PDF payment receipts.
    - Preserves business authorization and ownership controls.
    """
    queryset = Payment.objects.all().select_related('customer', 'company', 'invoice').prefetch_related('allocations__invoice')
    serializer_class = PaymentSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwnerUser]

    def get_queryset(self):
        queryset = super().get_queryset()
        customer_id = self.request.query_params.get('customer')
        method = self.request.query_params.get('method')
        date_param = self.request.query_params.get('date')

        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        if method:
            queryset = queryset.filter(method=method)
        if date_param:
            queryset = queryset.filter(received_at__date=date_param)

        return queryset

    @action(detail=True, methods=['post'])
    def reverse(self, request, pk=None):
        """
        POST /api/v1/billing/payments/{id}/reverse/
        Safely and atomically reverses a payment:
        - Requires a mandatory 'reason'
        - Undoes invoice allocations and restores invoice balance
        - Recalculates customer outstanding balance
        - Excludes reversed payment from future collection totals
        - Preserves immutable audit history
        """
        payment = self.get_object()
        reason = request.data.get('reason')
        if not reason or not str(reason).strip():
            return Response(
                {'reason': "A mandatory reversal reason must be provided."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            reversed_payment = PaymentAllocationService.reverse_payment(
                payment_id=payment.id,
                reason=str(reason).strip(),
                reversed_by=request.user
            )
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            PaymentSerializer(reversed_payment).data,
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['get'], renderer_classes=[PassthroughPdfRenderer])
    def receipt(self, request, pk=None):
        """
        GET /api/v1/billing/payments/{id}/receipt/
        Generates and streams an individual branded PDF payment receipt.
        """
        payment = self.get_object()
        pdf_bytes = generate_payment_receipt_pdf(payment)

        filename = f"ChaiFlow_Receipt_{payment.id}_{payment.received_at.strftime('%Y-%m-%d')}.pdf"
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        response['Content-Length'] = len(pdf_bytes)
        return response


