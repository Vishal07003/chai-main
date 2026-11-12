from rest_framework import serializers
from .models import DeviceToken

class DeviceTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceToken
        fields = ['id', 'token', 'platform', 'is_active', 'updated_at']
        read_only_fields = ['id', 'updated_at']

    def create(self, validated_data):
        user = self.context['request'].user
        token = validated_data['token']
        platform = validated_data.get('platform', 'android')

        # Update or create ensuring user owns their token and it stays active
        device, _ = DeviceToken.objects.update_or_create(
            token=token,
            defaults={'user': user, 'platform': platform, 'is_active': True}
        )
        return device

