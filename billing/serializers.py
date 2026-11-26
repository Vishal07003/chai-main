from rest_framework import serializers
from decimal import Decimal
from django.db import transaction, models
from django.utils import timezone
from .models import (
    Company, CompanyProductPrice, Customer, CustomerProductPrice,
    Invoice, InvoiceItem, Payment, PaymentAllocation, LedgerAdjustment,
    InvoiceOrder, DraftInvoice, DraftInvoiceItem, DraftInvoiceOrder,
    Expense, InvoiceAuditLog
)
from products.models import Product

class CompanyProductPriceSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)

    class Meta:
        model = CompanyProductPrice
        fields = [
            'id', 'company', 'product', 'product_name', 'custom_price',
            'min_quantity', 'valid_from', 'valid_to', 'is_active'
        ]

class CompanySerializer(serializers.ModelSerializer):
    total_invoices_count = serializers.SerializerMethodField()
    total_billed = serializers.SerializerMethodField()
    total_paid = serializers.SerializerMethodField()
    current_outstanding = serializers.SerializerMethodField()
    contracted_prices = CompanyProductPriceSerializer(many=True, read_only=True)

    class Meta:
        model = Company
        fields = [
            'id', 'name', 'trade_name', 'phone', 'email', 'gstin',
            'billing_address', 'billing_cycle', 'credit_limit',
            'payment_terms_days', 'is_active', 'created_at',
            'total_invoices_count', 'total_billed', 'total_paid', 'current_outstanding',
            'contracted_prices'
        ]
        read_only_fields = ['id', 'created_at', 'total_invoices_count', 'total_billed', 'total_paid', 'current_outstanding']

    def get_total_invoices_count(self, obj):
        return obj.invoices.exclude(status='void').count()

    def get_total_billed(self, obj):
        res = obj.invoices.exclude(status='void').aggregate(s=models.Sum('grand_total'))['s']
        return str(res or Decimal('0.00'))

    def get_total_paid(self, obj):
        res = Payment.objects.filter(
            models.Q(company=obj) | models.Q(invoice__company=obj)
        ).distinct().aggregate(s=models.Sum('amount'))['s']
        return str(res or Decimal('0.00'))

    def get_current_outstanding(self, obj):
        billed = Decimal(self.get_total_billed(obj))
        paid = Decimal(self.get_total_paid(obj))
        return str(max(Decimal('0.00'), billed - paid))


class CustomerProductPriceSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)

    class Meta:
        model = CustomerProductPrice
        fields = [
            'id', 'customer', 'product', 'product_name', 'custom_price',
            'is_active', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class CustomerSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(required=False, allow_blank=True, default='')
    address = serializers.CharField(required=False, allow_blank=True, default='')
    total_bills = serializers.SerializerMethodField()
    total_billed_amount = serializers.SerializerMethodField()
    total_paid_amount = serializers.SerializerMethodField()
    total_pending_amount = serializers.SerializerMethodField()
    is_defaulter = serializers.SerializerMethodField()
    has_overdue = serializers.SerializerMethodField()
    custom_prices = CustomerProductPriceSerializer(many=True, read_only=True)

    class Meta:
        model = Customer
        fields = [
            'id', 'name', 'phone', 'address', 'created_at',
            'total_bills', 'total_billed_amount', 'total_paid_amount', 'total_pending_amount',
            'is_defaulter', 'has_overdue',
            'custom_prices'
        ]
        read_only_fields = ['id', 'created_at', 'total_bills', 'total_billed_amount', 'total_paid_amount', 'total_pending_amount', 'is_defaulter', 'has_overdue', 'custom_prices']

    def validate_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Customer Name cannot be empty.")
        return value.strip()

    def validate_phone(self, value):
        val = (value or '').strip()
        if not val:
            return ''
        import re
        cleaned = re.sub(r'\D', '', val)
        if len(cleaned) == 12 and cleaned.startswith('91'):
            cleaned = cleaned[2:]
        if len(cleaned) != 10 or not cleaned[0] in '6789':
            raise serializers.ValidationError("Please enter a valid 10-digit mobile number.")
        
        # Check duplicate only if phone is provided
        qs = Customer.objects.filter(models.Q(phone=val) | models.Q(phone=cleaned) | models.Q(phone=f"+91{cleaned}"))
        if self.instance:
            qs = qs.exclude(id=self.instance.id)
        if qs.exists():
            raise serializers.ValidationError("A customer with this mobile number is already registered.")
        return val

    def get_total_bills(self, obj):
        return obj.invoices.exclude(status='void').count()

    def get_total_billed_amount(self, obj):
        res = obj.invoices.exclude(status='void').aggregate(total=models.Sum('grand_total'))['total']
        return str(res or Decimal('0.00'))

    def get_total_paid_amount(self, obj):
        res = Payment.objects.filter(models.Q(customer=obj) | models.Q(invoice__customer=obj)).distinct().aggregate(total=models.Sum('amount'))['total']
        return str(res or Decimal('0.00'))

    def get_total_pending_amount(self, obj):
        pending = sum(inv.pending_amount for inv in obj.invoices.exclude(status='void').all())
        return str(pending)

    def get_has_overdue(self, obj):
        pending = Decimal(self.get_total_pending_amount(obj))
        if pending <= Decimal('0.00'):
            return False
        today = timezone.now().date()
        for inv in obj.invoices.exclude(status='void'):
            if inv.pending_amount > Decimal('0.00') and inv.due_date is not None and inv.due_date < today:
                return True
        return False

    def get_is_defaulter(self, obj):
        return self.get_has_overdue(obj)


class PaymentAllocationSerializer(serializers.ModelSerializer):
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)

    class Meta:
        model = PaymentAllocation
        fields = ['id', 'payment', 'invoice', 'invoice_number', 'allocated_amount', 'allocated_at', 'notes']
        read_only_fields = ['id', 'allocated_at']


class PaymentSerializer(serializers.ModelSerializer):
    allocations = PaymentAllocationSerializer(many=True, read_only=True)
    unallocated_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = Payment
        fields = [
            'id', 'company', 'invoice', 'amount', 'method', 'reference_number',
            'received_at', 'notes', 'allocations', 'unallocated_amount'
        ]
        read_only_fields = ['id', 'received_at', 'allocations', 'unallocated_amount']

    def validate_amount(self, value):
        if value <= Decimal('0.00'):
            raise serializers.ValidationError("Payment amount must be greater than zero.")
        return value


class InvoiceItemSerializer(serializers.ModelSerializer):
    has_cost_snapshot = serializers.BooleanField(read_only=True)

    class Meta:
        model = InvoiceItem
        fields = [
            'id', 'product', 'product_name', 'unit_price', 'quantity', 'total_price',
            'unit_cost', 'total_cost', 'has_cost_snapshot'
        ]
        read_only_fields = ['id', 'total_price', 'unit_cost', 'total_cost', 'has_cost_snapshot']


class InvoiceAuditLogSerializer(serializers.ModelSerializer):
    edited_by_name = serializers.CharField(source='edited_by.username', read_only=True)

    class Meta:
        model = InvoiceAuditLog
        fields = [
            'id', 'invoice', 'edited_by', 'edited_by_name',
            'previous_total', 'updated_total',
            'previous_values', 'updated_values',
            'reason', 'created_at'
        ]
        read_only_fields = ['id', 'invoice', 'edited_by', 'edited_by_name', 'created_at']


class InvoiceDetailSerializer(serializers.ModelSerializer):
    items = InvoiceItemSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    allocations = PaymentAllocationSerializer(many=True, read_only=True)
    audit_logs = InvoiceAuditLogSerializer(many=True, read_only=True)
    total_paid = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    pending_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    company_name = serializers.CharField(source='company.name', read_only=True)
    is_overdue = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'invoice_number', 'order', 'billing_type', 'billing_period', 'company', 'company_name',
            'customer', 'customer_name', 'customer_phone',
            'subtotal', 'discount_amount', 'grand_total', 'status', 'due_date', 'finalized_at', 'notes', 'created_at',
            'items', 'payments', 'allocations', 'audit_logs', 'total_paid', 'pending_amount', 'is_overdue'
        ]
        read_only_fields = [
            'id', 'invoice_number', 'order', 'billing_type', 'billing_period', 'created_at', 'status', 'finalized_at',
            'total_paid', 'pending_amount', 'company_name', 'is_overdue', 'audit_logs'
        ]

    def get_is_overdue(self, obj):
        if obj.status == 'paid' or obj.pending_amount <= Decimal('0.00'):
            return False
        if obj.due_date is None:
            return False
        today = timezone.now().date()
        return obj.due_date < today


class InvoiceCreateItemSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(required=False, allow_null=True)
    product_name = serializers.CharField(max_length=200, required=False)
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    quantity = serializers.IntegerField(min_value=1)


class InvoiceCreateSerializer(serializers.Serializer):
    """
    Used for Quick Billing & Walk-In sales:
    - Never generates duplicate invoices
    - Recalculates product prices on backend
    """
    company_id = serializers.IntegerField(required=False, allow_null=True)
    customer_id = serializers.IntegerField(required=False, allow_null=True)
    customer_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    customer_phone = serializers.CharField(max_length=15, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    discount_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=Decimal('0.00')
    )
    items = InvoiceCreateItemSerializer(many=True)
    
    # Optional upfront payment
    initial_payment_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=Decimal('0.00')
    )
    initial_payment_method = serializers.ChoiceField(
        choices=Payment.METHOD_CHOICES, required=False, default='cash'
    )

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Invoice must contain at least one product.")
        return value

    def validate_discount_amount(self, value):
        if value is not None and value < Decimal('0.00'):
            raise serializers.ValidationError("Discount amount cannot be negative.")
        return value or Decimal('0.00')

    def create(self, validated_data):
        company_id = validated_data.get('company_id')
        customer_id = validated_data.get('customer_id')
        items_data = validated_data['items']
        notes = validated_data.get('notes', '')
        discount_amount = validated_data.get('discount_amount', Decimal('0.00'))
        init_amount = validated_data.get('initial_payment_amount', Decimal('0.00'))
        init_method = validated_data.get('initial_payment_method', 'cash')

        company = None
        if company_id:
            try:
                company = Company.objects.get(id=company_id)
            except Company.DoesNotExist:
                raise serializers.ValidationError({'company_id': 'Company does not exist.'})

        customer = None
        if customer_id:
            try:
                customer = Customer.objects.get(id=customer_id)
            except Customer.DoesNotExist:
                raise serializers.ValidationError({'customer_id': 'Customer does not exist.'})

        c_name = company.name if company else (customer.name if customer else (validated_data.get('customer_name') or 'Walk-in Customer'))
        c_phone = company.phone if company else (customer.phone if customer else (validated_data.get('customer_phone') or ''))

        with transaction.atomic():
            today_str = timezone.now().strftime('%Y%m%d')
            today_invoices = Invoice.objects.filter(invoice_number__startswith=f"INV-{today_str}-")
            max_num = 0
            for ti in today_invoices:
                try:
                    num_part = int(ti.invoice_number.split('-')[-1])
                    if num_part > max_num:
                        max_num = num_part
                except (ValueError, IndexError):
                    pass
            inv_number = f"INV-{today_str}-{(max_num + 1):04d}"

            invoice = Invoice.objects.create(
                invoice_number=inv_number,
                company=company,
                customer=customer,
                customer_name=c_name,
                customer_phone=c_phone,
                notes=notes,
                subtotal=Decimal('0.00'),
                grand_total=Decimal('0.00'),
            )

            product_ids = [it['product_id'] for it in items_data if it.get('product_id')]
            products_map = {p.id: p for p in Product.objects.filter(id__in=product_ids)}

            subtotal = Decimal('0.00')
            for item in items_data:
                pid = item.get('product_id')
                qty = item['quantity']

                if pid and pid in products_map:
                    p = products_map[pid]
                    p_name = p.name
                    # Backend price check
                    u_price = p.price
                else:
                    p_name = item.get('product_name', 'Item')
                    u_price = item.get('unit_price', Decimal('0.00'))

                item_total = u_price * qty
                subtotal += item_total

                # Phase R1: Snapshot unit_cost & total_cost
                prod_obj = products_map.get(pid) if pid else None
                unit_cost_val = getattr(prod_obj, 'cost_price', None) if prod_obj else None
                total_cost_val = (unit_cost_val * qty) if unit_cost_val is not None else None

                InvoiceItem.objects.create(
                    invoice=invoice,
                    product_id=pid if pid in products_map else None,
                    product_name=p_name,
                    unit_price=u_price,
                    quantity=qty,
                    total_price=item_total,
                    unit_cost=unit_cost_val,
                    total_cost=total_cost_val,
                )

            if discount_amount > subtotal:
                raise serializers.ValidationError({
                    'discount_amount': f"Discount amount (₹{discount_amount}) cannot exceed subtotal (₹{subtotal})."
                })

            invoice.subtotal = subtotal
            invoice.discount_amount = discount_amount
            invoice.grand_total = max(Decimal('0.00'), subtotal - discount_amount)
            invoice.save(update_fields=['subtotal', 'discount_amount', 'grand_total'])

            # Record upfront payment if provided
            if init_amount > Decimal('0.00'):
                if init_amount > invoice.grand_total:
                    raise serializers.ValidationError({
                        'initial_payment_amount': f"Payment amount (₹{init_amount}) cannot exceed total bill amount (₹{invoice.grand_total})."
                    })
                from .payment_service import PaymentAllocationService
                pm = PaymentAllocationService.record_payment(
                    amount=init_amount,
                    method=init_method,
                    customer=customer,
                    company=company,
                    invoice=invoice,
                    notes='Initial billing payment'
                )
                PaymentAllocationService.allocate_payment_fifo(
                    payment=pm,
                    target_invoices=[invoice]
                )
            
            invoice.update_payment_status()

        return invoice


class LedgerAdjustmentSerializer(serializers.ModelSerializer):
    approved_by_name = serializers.CharField(source='approved_by.username', read_only=True)

    class Meta:
        model = LedgerAdjustment
        fields = [
            'id', 'company', 'adjustment_type', 'amount', 'reason',
            'approved_by', 'approved_by_name', 'created_at'
        ]
        read_only_fields = ['id', 'approved_by', 'approved_by_name', 'created_at']


# --------------------------------------------------------------------------
# Phase 2: Draft Billing Serializers
# --------------------------------------------------------------------------

class DraftInvoiceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = DraftInvoiceItem
        fields = [
            'id', 'product', 'product_name', 'unit', 'quantity', 'unit_price', 'total_price',
            'unit_cost', 'total_cost'
        ]
        read_only_fields = ['id', 'total_price', 'unit_cost', 'total_cost']


class DraftInvoiceOrderSerializer(serializers.ModelSerializer):
    order_id = serializers.IntegerField(source='order.id', read_only=True)
    order_total = serializers.DecimalField(source='order.total_amount', max_digits=10, decimal_places=2, read_only=True)
    order_status = serializers.CharField(source='order.status', read_only=True)
    order_date = serializers.DateTimeField(source='order.created_at', read_only=True)

    class Meta:
        model = DraftInvoiceOrder
        fields = ['id', 'order_id', 'order_total', 'order_status', 'order_date', 'reserved_at']
        read_only_fields = ['id', 'reserved_at']


class DraftInvoiceDetailSerializer(serializers.ModelSerializer):
    items = DraftInvoiceItemSerializer(many=True, read_only=True)
    draft_orders = DraftInvoiceOrderSerializer(many=True, read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    official_invoice_number = serializers.CharField(source='official_invoice.invoice_number', read_only=True)
    delivery_partner_id = serializers.IntegerField(source='delivery_partner.id', read_only=True, allow_null=True)
    delivery_partner_name = serializers.CharField(source='delivery_partner.name', read_only=True, allow_null=True)

    class Meta:
        model = DraftInvoice
        fields = [
            'id', 'draft_number', 'customer', 'customer_name', 'customer_phone',
            'billing_source', 'status', 'subtotal', 'discount_amount', 'grand_total',
            'due_date', 'notes', 'delivery_partner', 'delivery_partner_id', 'delivery_partner_name',
            'delivery_address',
            'expires_at', 'created_by', 'created_by_name',
            'official_invoice', 'official_invoice_number', 'created_at', 'updated_at',
            'items', 'draft_orders'
        ]
        read_only_fields = [
            'id', 'draft_number', 'status', 'subtotal', 'grand_total',
            'delivery_partner', 'delivery_partner_id', 'delivery_partner_name',
            'created_by', 'created_by_name', 'official_invoice', 'official_invoice_number',
            'created_at', 'updated_at'
        ]


class DraftInvoiceItemInputSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(required=False, allow_null=True)
    product_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    unit = serializers.CharField(max_length=50, required=False, default='Pc')
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.00'))


class DraftInvoiceCreateSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField(required=False, allow_null=True)
    delivery_partner_id = serializers.IntegerField(required=False, allow_null=True)
    delivery_address = serializers.CharField(required=False, allow_blank=True, default='')
    billing_source = serializers.ChoiceField(
        choices=DraftInvoice.BILLING_SOURCE_CHOICES, default='quick_billing'
    )
    items = DraftInvoiceItemInputSerializer(many=True, required=False, default=list)
    order_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list
    )
    discount_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=Decimal('0.00'), min_value=Decimal('0.00')
    )
    due_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_delivery_partner_id(self, value):
        if value is not None:
            from deliveries.models import DeliveryPartner
            try:
                partner = DeliveryPartner.objects.get(id=value)
            except DeliveryPartner.DoesNotExist:
                raise serializers.ValidationError("Delivery partner does not exist.")
            if not partner.is_active:
                raise serializers.ValidationError("Selected delivery partner is inactive.")
        return value

    def validate(self, attrs):
        billing_source = attrs.get('billing_source', 'quick_billing')
        order_ids = attrs.get('order_ids', [])
        items = attrs.get('items', [])
        delivery_partner_id = attrs.get('delivery_partner_id')
        customer_id = attrs.get('customer_id')
        delivery_address = (attrs.get('delivery_address') or '').strip()

        if billing_source in ('single_order', 'consolidated_orders') and not order_ids:
            raise serializers.ValidationError({
                'order_ids': f"At least one order ID must be provided for '{billing_source}'."
            })

        if billing_source == 'quick_billing' and not items:
            raise serializers.ValidationError({
                'items': "At least one product item is required for Quick Billing drafts."
            })

        # Phase 5C.1 Validation: Quick billing drafts with assigned delivery partner require a valid destination address
        if billing_source == 'quick_billing' and delivery_partner_id is not None:
            if not customer_id:
                # Walk-in customer
                if not delivery_address:
                    raise serializers.ValidationError({
                        'delivery_address': "Delivery address is required when assigning a delivery partner to a walk-in customer."
                    })
            else:
                # Registered customer
                from billing.models import Customer
                cust = Customer.objects.filter(id=customer_id).first()
                effective_address = delivery_address or (cust.address.strip() if (cust and cust.address) else '')
                if not effective_address:
                    raise serializers.ValidationError({
                        'delivery_address': "Delivery address is required for delivery orders. Customer has no saved address, please provide an address."
                    })

        return attrs


class DraftInvoiceUpdateSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField(required=False, allow_null=True)
    delivery_partner_id = serializers.IntegerField(required=False, allow_null=True)
    delivery_address = serializers.CharField(required=False, allow_blank=True)
    items = DraftInvoiceItemInputSerializer(many=True, required=False)
    order_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False
    )
    discount_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, min_value=Decimal('0.00')
    )
    due_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_delivery_partner_id(self, value):
        if value is not None:
            from deliveries.models import DeliveryPartner
            try:
                partner = DeliveryPartner.objects.get(id=value)
            except DeliveryPartner.DoesNotExist:
                raise serializers.ValidationError("Delivery partner does not exist.")
            if not partner.is_active:
                raise serializers.ValidationError("Selected delivery partner is inactive.")
        return value


class DraftFinalizeSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True)


class ExpenseSerializer(serializers.ModelSerializer):
    recorded_by_name = serializers.CharField(source='recorded_by.username', read_only=True)
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    payment_method_display = serializers.CharField(source='get_payment_method_display', read_only=True)

    class Meta:
        model = Expense
        fields = [
            'id', 'title', 'category', 'category_display', 'amount',
            'expense_date', 'payment_method', 'payment_method_display',
            'notes', 'recorded_by', 'recorded_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'recorded_by', 'recorded_by_name', 'created_at', 'updated_at']

    def validate_title(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Expense title is required.")
        return value.strip()

    def validate_amount(self, value):
        if value is None or value <= Decimal('0.00'):
            raise serializers.ValidationError("Expense amount must be greater than zero.")
        return value

