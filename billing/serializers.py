from rest_framework import serializers
from decimal import Decimal
from django.db import transaction, models
from django.utils import timezone
from .models import Company, CompanyProductPrice, Customer, Invoice, InvoiceItem, Payment, PaymentAllocation, LedgerAdjustment
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


class CustomerSerializer(serializers.ModelSerializer):
    total_bills = serializers.SerializerMethodField()
    total_billed_amount = serializers.SerializerMethodField()
    total_pending_amount = serializers.SerializerMethodField()
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = Customer
        fields = [
            'id', 'name', 'phone', 'address', 'company', 'company_name', 'created_at',
            'total_bills', 'total_billed_amount', 'total_pending_amount'
        ]
        read_only_fields = ['id', 'created_at', 'total_bills', 'total_billed_amount', 'total_pending_amount']

    def get_total_bills(self, obj):
        return obj.invoices.count()

    def get_total_billed_amount(self, obj):
        res = obj.invoices.aggregate(total=models.Sum('grand_total'))['total']
        return str(res or Decimal('0.00'))

    def get_total_pending_amount(self, obj):
        pending = sum(inv.pending_amount for inv in obj.invoices.all())
        return str(pending)


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
    class Meta:
        model = InvoiceItem
        fields = ['id', 'product', 'product_name', 'unit_price', 'quantity', 'total_price']
        read_only_fields = ['id', 'total_price']


class InvoiceDetailSerializer(serializers.ModelSerializer):
    items = InvoiceItemSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    allocations = PaymentAllocationSerializer(many=True, read_only=True)
    total_paid = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    pending_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = Invoice
        fields = [
            'id', 'invoice_number', 'order', 'company', 'company_name',
            'customer', 'customer_name', 'customer_phone',
            'subtotal', 'discount_amount', 'grand_total', 'status', 'notes', 'created_at',
            'items', 'payments', 'allocations', 'total_paid', 'pending_amount'
        ]
        read_only_fields = [
            'id', 'invoice_number', 'order', 'created_at', 'status',
            'total_paid', 'pending_amount', 'company_name'
        ]


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

    def create(self, validated_data):
        company_id = validated_data.get('company_id')
        customer_id = validated_data.get('customer_id')
        items_data = validated_data['items']
        notes = validated_data.get('notes', '')
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
            count_today = Invoice.objects.filter(created_at__date=timezone.now().date()).count() + 1
            inv_number = f"INV-{today_str}-{count_today:04d}"

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

                InvoiceItem.objects.create(
                    invoice=invoice,
                    product_id=pid if pid in products_map else None,
                    product_name=p_name,
                    unit_price=u_price,
                    quantity=qty,
                    total_price=item_total,
                )

            invoice.subtotal = subtotal
            invoice.grand_total = subtotal
            invoice.save(update_fields=['subtotal', 'grand_total'])

            # Record upfront payment if provided
            if init_amount > Decimal('0.00'):
                if init_amount > invoice.grand_total:
                    raise serializers.ValidationError({
                        'initial_payment_amount': f"Payment amount (₹{init_amount}) cannot exceed total bill amount (₹{invoice.grand_total})."
                    })
                pm = Payment.objects.create(
                    company=company,
                    invoice=invoice,
                    amount=init_amount,
                    method=init_method,
                    notes='Initial billing payment'
                )
                PaymentAllocation.objects.create(
                    payment=pm,
                    invoice=invoice,
                    allocated_amount=init_amount,
                    notes='Initial full/partial payment allocation'
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
