from rest_framework import serializers
from decimal import Decimal
from django.db import transaction
from .models import Order, OrderItem, PriceOverrideRequest
from products.models import Product
from products.serializers import ProductSerializer

class OrderItemReadSerializer(serializers.ModelSerializer):
    product_details = ProductSerializer(source='product', read_only=True)

    class Meta:
        model = OrderItem
        fields = ['id', 'product', 'product_details', 'quantity', 'price']
        read_only_fields = ['id', 'product', 'product_details', 'quantity', 'price']

class OrderItemWriteSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)
    requested_price = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    discount_reason = serializers.CharField(required=False, allow_blank=True)

class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemReadSerializer(many=True, read_only=True)
    customer_name = serializers.ReadOnlyField(source='customer.username')
    company_name = serializers.ReadOnlyField(source='company.name')
    has_invoice = serializers.BooleanField(read_only=True)
    invoice_number = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'customer', 'customer_name', 'company', 'company_name',
            'status', 'total_amount', 'delivery_address', 'notes',
            'has_invoice', 'invoice_number', 'created_at', 'updated_at', 'items'
        ]
        read_only_fields = ['id', 'customer', 'customer_name', 'company_name', 'total_amount', 'has_invoice', 'invoice_number', 'created_at', 'updated_at']

    def get_invoice_number(self, obj):
        if hasattr(obj, 'invoice') and obj.invoice:
            return obj.invoice.invoice_number
        return None

class OrderCreateSerializer(serializers.ModelSerializer):
    company_id = serializers.IntegerField(required=False, allow_null=True)
    items = OrderItemWriteSerializer(many=True, write_only=True)

    class Meta:
        model = Order
        fields = ['id', 'company_id', 'delivery_address', 'notes', 'items']

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Order must include at least one item.")
        return value

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        company_id = validated_data.pop('company_id', None)
        user = self.context['request'].user

        company = None
        if company_id:
            from billing.models import Company
            try:
                company = Company.objects.get(id=company_id)
            except Company.DoesNotExist:
                raise serializers.ValidationError({'company_id': 'Company does not exist.'})

        with transaction.atomic():
            total = Decimal('0.00')
            order = Order.objects.create(
                customer=user,
                company=company,
                total_amount=Decimal('0.00'),
                **validated_data
            )

            product_ids = [item['product_id'] for item in items_data]
            products = {p.id: p for p in Product.objects.filter(id__in=product_ids)}

            from billing.invoicing_service import InvoicingService

            for item_data in items_data:
                pid = item_data['product_id']
                qty = item_data['quantity']
                req_price = item_data.get('requested_price')
                disc_reason = item_data.get('discount_reason', '')

                if pid not in products:
                    raise serializers.ValidationError({'items': f"Product ID {pid} does not exist."})

                product = products[pid]
                standard_rate = InvoicingService.resolve_item_unit_price(company, product, qty)

                # If a discounted rate is requested, create a pending approval request
                final_item_price = standard_rate
                order_item = OrderItem.objects.create(
                    order=order,
                    product=product,
                    quantity=qty,
                    price=final_item_price
                )

                if req_price is not None and req_price < standard_rate:
                    PriceOverrideRequest.objects.create(
                        order=order,
                        order_item=order_item,
                        product=product,
                        standard_price=standard_rate,
                        requested_price=req_price,
                        discount_amount=(standard_rate - req_price) * qty,
                        quantity=qty,
                        reason=disc_reason or 'Special client pricing request',
                        status='PENDING',
                        requested_by=user,
                    )

                total += (final_item_price * qty)

            order.total_amount = total
            order.save(update_fields=['total_amount'])

            # Create initial delivery assignment container (without invoice generation)
            from deliveries.models import Delivery
            Delivery.objects.get_or_create(order=order)

            # Trigger FCM notification to Owner
            from notifications.services import NotificationService
            NotificationService.on_order_created(order)

        return order


class PriceOverrideRequestSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    requested_by_name = serializers.CharField(source='requested_by.username', read_only=True)
    reviewed_by_name = serializers.CharField(source='reviewed_by.username', read_only=True)

    class Meta:
        model = PriceOverrideRequest
        fields = [
            'id', 'order', 'order_item', 'product', 'product_name',
            'standard_price', 'requested_price', 'discount_amount',
            'quantity', 'reason', 'status', 'requested_by', 'requested_by_name',
            'reviewed_by', 'reviewed_by_name', 'decision_timestamp', 'review_notes', 'created_at'
        ]
        read_only_fields = [
            'id', 'order', 'order_item', 'product', 'standard_price',
            'discount_amount', 'quantity', 'requested_by', 'requested_by_name',
            'reviewed_by', 'reviewed_by_name', 'decision_timestamp', 'created_at'
        ]
