from rest_framework import serializers
from .models import (
    Delivery,
    DeliveryItem,
    DeliveryPartner,
    DeliverySchedule,
    DeliveryLocationLog,
    DeliveryStatusHistory,
)
from orders.serializers import OrderSerializer
from django.contrib.auth.models import User

class DriverSummarySerializer(serializers.ModelSerializer):
    phone = serializers.ReadOnlyField(source='profile.phone')

    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'phone']

class DeliveryItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)

    class Meta:
        model = DeliveryItem
        fields = [
            'id', 'delivery', 'product', 'product_name',
            'ordered_quantity', 'delivered_quantity', 'discrepancy_notes',
            'has_discrepancy',
        ]
        read_only_fields = ['id', 'product_name', 'has_discrepancy']

class DeliveryLocationLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryLocationLog
        fields = ['id', 'delivery', 'driver', 'latitude', 'longitude', 'accuracy', 'speed', 'recorded_at']
        read_only_fields = ['id', 'driver']

class DeliveryStatusHistorySerializer(serializers.ModelSerializer):
    changed_by_name = serializers.CharField(source='changed_by.username', read_only=True)

    class Meta:
        model = DeliveryStatusHistory
        fields = ['id', 'delivery', 'from_status', 'to_status', 'changed_by', 'changed_by_name', 'notes', 'created_at']
        read_only_fields = ['id', 'changed_by', 'changed_by_name', 'created_at']

class DeliverySerializer(serializers.ModelSerializer):
    order_details = OrderSerializer(source='order', read_only=True)
    driver_details = DriverSummarySerializer(source='driver', read_only=True)
    delivered_items = DeliveryItemSerializer(many=True, read_only=True)
    status_history = DeliveryStatusHistorySerializer(many=True, read_only=True)

    class Meta:
        model = Delivery
        fields = [
            'id', 'order', 'order_details', 'driver', 'driver_details',
            'status', 'pickup_time', 'delivered_time', 'current_lat',
            'current_lng', 'failure_reason', 'updated_at',
            'delivered_items', 'status_history',
        ]
        read_only_fields = ['id', 'order', 'updated_at', 'delivered_items', 'status_history']

    def validate_driver(self, value):
        if value is not None and hasattr(value, 'profile') and value.profile.role != 'delivery':
            raise serializers.ValidationError("Assigned user must have the 'delivery' role.")
        return value

class DeliveryPartnerSerializer(serializers.ModelSerializer):
    active_deliveries_count = serializers.SerializerMethodField()

    class Meta:
        model = DeliveryPartner
        fields = [
            'id', 'user', 'name', 'phone', 'emergency_phone',
            'license_number', 'vehicle_type', 'vehicle_model',
            'number_plate', 'location', 'status', 'avatar_url',
            'active_deliveries_count', 'created_at',
        ]
        read_only_fields = ['id', 'active_deliveries_count', 'created_at']

    def get_active_deliveries_count(self, obj):
        if obj.user:
            return Delivery.objects.filter(driver=obj.user).exclude(status__in=['delivered', 'failed']).count()
        return 0

class DeliveryScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliverySchedule
        fields = [
            'id', 'time_slot', 'title', 'customer_name', 'address',
            'partner', 'partner_name', 'status', 'items_count',
            'scheduled_date', 'created_at',
        ]
        read_only_fields = ['id', 'scheduled_date', 'created_at']
