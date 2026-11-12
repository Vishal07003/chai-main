from rest_framework import serializers
from decimal import Decimal
from django.db import transaction
from .models import Order, OrderItem
from products.models import Product
from products.serializers import ProductSerializer

class OrderItemReadSerializer(serializers.ModelSerializer):
    product_details = ProductSerializer(source='product', read_only=True)

    class Meta:
        model = OrderItem
        fields = ['id', 'product', 'product_details', 'quantity', 'price']

class OrderItemWriteSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)

class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemReadSerializer(many=True, read_only=True)
    customer_name = serializers.ReadOnlyField(source='customer.username')

    class Meta:
        model = Order
        fields = [
            'id', 'customer', 'customer_name', 'status', 'total_amount',
            'delivery_address', 'notes', 'created_at', 'updated_at', 'items'
        ]
        read_only_fields = ['id', 'customer', 'total_amount', 'created_at', 'updated_at']

class OrderCreateSerializer(serializers.ModelSerializer):
    items = OrderItemWriteSerializer(many=True, write_only=True)

    class Meta:
        model = Order
        fields = ['id', 'delivery_address', 'notes', 'items']

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Order must include at least one item.")
        return value

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        user = self.context['request'].user

        with transaction.atomic():
            total = Decimal('0.00')
            order = Order.objects.create(
                customer=user,
                total_amount=Decimal('0.00'),
                **validated_data
            )

            product_ids = [item['product_id'] for item in items_data]
            products = {p.id: p for p in Product.objects.filter(id__in=product_ids)}

            for item_data in items_data:
                pid = item_data['product_id']
                qty = item_data['quantity']

                if pid not in products:
                    raise serializers.ValidationError(f"Product with ID {pid} does not exist.")

                product = products[pid]
                if not product.is_available:
                    raise serializers.ValidationError(f"Product '{product.name}' is currently unavailable.")

                item_price = product.price
                OrderItem.objects.create(
                    order=order,
                    product=product,
                    quantity=qty,
                    price=item_price
                )
                total += item_price * qty

            order.total_amount = total
            order.save()

            # Automatically create the initial delivery record for this order
            from deliveries.models import Delivery
            Delivery.objects.get_or_create(order=order)

        return order
