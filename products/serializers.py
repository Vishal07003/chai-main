from rest_framework import serializers
from decimal import Decimal
from .models import Category, Product

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = '__all__'

class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.ReadOnlyField(source='category.name')

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'category', 'category_name', 'price', 'cost_price',
            'unit', 'description', 'image_url', 'is_available', 'created_at'
        ]

    def validate_price(self, value):
        if value is not None and value < Decimal('0.00'):
            raise serializers.ValidationError("Selling price cannot be negative.")
        return value

    def validate_cost_price(self, value):
        if value is not None and value < Decimal('0.00'):
            raise serializers.ValidationError("Cost price cannot be negative.")
        return value or Decimal('0.00')

