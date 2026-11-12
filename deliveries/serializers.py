from rest_framework import serializers
from .models import Delivery, DeliveryPartner, DeliverySchedule
from orders.serializers import OrderSerializer
from django.contrib.auth.models import User

class DriverSummarySerializer(serializers.ModelSerializer):
    phone = serializers.ReadOnlyField(source='profile.phone')

    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'phone']

class DeliverySerializer(serializers.ModelSerializer):
    order_details = OrderSerializer(source='order', read_only=True)
    driver_details = DriverSummarySerializer(source='driver', read_only=True)

    class Meta:
        model = Delivery
        fields = [
            'id', 'order', 'order_details', 'driver', 'driver_details',
            'status', 'pickup_time', 'delivered_time', 'current_lat',
            'current_lng', 'updated_at'
        ]
        read_only_fields = ['id', 'order', 'updated_at']

    def validate_driver(self, value):
        if value is not None and hasattr(value, 'profile') and value.profile.role != 'delivery':
            raise serializers.ValidationError("Assigned user must have the 'delivery' role.")
        return value

class DeliveryPartnerSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryPartner
        fields = [
            'id', 'user', 'name', 'phone', 'emergency_phone',
            'license_number', 'vehicle_type', 'vehicle_model',
            'number_plate', 'location', 'status', 'avatar_url',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']

class DeliveryScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliverySchedule
        fields = [
            'id', 'time_slot', 'title', 'customer_name', 'address',
            'partner', 'partner_name', 'status', 'items_count',
            'scheduled_date', 'created_at',
        ]
        read_only_fields = ['id', 'scheduled_date', 'created_at']
