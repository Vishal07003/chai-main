from rest_framework import serializers
from .models import DeviceToken, NotificationLog

class DeviceTokenSerializer(serializers.ModelSerializer):
    # Override token field to remove automatic UniqueValidator from ModelSerializer,
    # allowing update_or_create to seamlessly handle re-registration without HTTP 400.
    token = serializers.CharField(max_length=255, required=True)

    class Meta:
        model = DeviceToken
        fields = ('id', 'token', 'platform', 'is_active', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def create(self, validated_data):
        # Associate token strictly with request.user
        validated_data['user'] = self.context['request'].user
        # Re-activate token if re-registered or transfer if user changed on same device
        token, created = DeviceToken.objects.update_or_create(
            token=validated_data['token'],
            defaults={
                'user': validated_data['user'],
                'platform': validated_data.get('platform', 'android'),
                'is_active': True,
            }
        )
        return token


class NotificationLogSerializer(serializers.ModelSerializer):
    channel_display = serializers.CharField(source='get_channel_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = NotificationLog
        fields = (
            'id',
            'recipient',
            'target_role',
            'channel',
            'channel_display',
            'title',
            'body',
            'data_payload',
            'status',
            'status_display',
            'is_read',
            'retry_count',
            'sent_at',
            'created_at',
        )
        read_only_fields = (
            'id',
            'recipient',
            'target_role',
            'channel',
            'channel_display',
            'status',
            'status_display',
            'retry_count',
            'sent_at',
            'created_at',
        )
