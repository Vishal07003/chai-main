from rest_framework import serializers
from .models import Delivery
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
