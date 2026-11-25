from rest_framework import serializers
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from .models import (
    Order, OrderItem, PriceOverrideRequest, RecurringOrder, RecurringOrderItem,
    RecurringOrderExecution, GlobalRecurringNotificationSetting,
    DailyDeliverySchedule, DailyDeliveryScheduleItem, ScheduleCancellationException
)
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
    customer_name = serializers.SerializerMethodField()
    customer_phone = serializers.SerializerMethodField()
    delivery_partner_id = serializers.SerializerMethodField()
    delivery_partner_name = serializers.SerializerMethodField()
    delivery_status = serializers.SerializerMethodField()
    has_invoice = serializers.BooleanField(read_only=True)
    invoice_number = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'customer', 'customer_profile', 'customer_name', 'customer_phone', 'company',
            'status', 'total_amount', 'delivery_address', 'notes',
            'scheduled_delivery_date', 'scheduled_delivery_time',
            'delivery_partner_id', 'delivery_partner_name', 'delivery_status',
            'has_invoice', 'invoice_number', 'created_at', 'updated_at', 'items'
        ]
        read_only_fields = ['id', 'customer', 'customer_name', 'customer_phone', 'delivery_partner_id', 'delivery_partner_name', 'delivery_status', 'total_amount', 'has_invoice', 'invoice_number', 'created_at', 'updated_at']

    def get_customer_name(self, obj):
        if obj.customer_profile:
            return obj.customer_profile.name
        if obj.company:
            return obj.company.name
        return obj.customer.get_full_name() or obj.customer.username

    def get_customer_phone(self, obj):
        if obj.customer_profile and obj.customer_profile.phone:
            return obj.customer_profile.phone
        if obj.company and obj.company.phone:
            return obj.company.phone
        if hasattr(obj.customer, 'profile') and obj.customer.profile.phone:
            return obj.customer.profile.phone
        return ''

    def get_delivery_partner_id(self, obj):
        if hasattr(obj, 'delivery') and obj.delivery and obj.delivery.driver:
            return obj.delivery.driver.id
        return None

    def get_delivery_partner_name(self, obj):
        if hasattr(obj, 'delivery') and obj.delivery and obj.delivery.driver:
            driver = obj.delivery.driver
            if hasattr(driver, 'partner_profile') and driver.partner_profile:
                return driver.partner_profile.name
            full_name = f"{driver.first_name} {driver.last_name}".strip()
            return full_name or driver.username
        return None

    def get_delivery_status(self, obj):
        if hasattr(obj, 'delivery') and obj.delivery:
            return obj.delivery.status
        return None

    def get_invoice_number(self, obj):
        if hasattr(obj, 'invoice') and obj.invoice:
            return obj.invoice.invoice_number
        return None

class OrderCreateSerializer(serializers.ModelSerializer):
    customer_id = serializers.IntegerField(required=False, allow_null=True)
    company_id = serializers.IntegerField(required=False, allow_null=True)
    delivery_partner_id = serializers.IntegerField(required=False, allow_null=True)
    items = OrderItemWriteSerializer(many=True, write_only=True)

    class Meta:
        model = Order
        fields = ['id', 'customer_id', 'company_id', 'delivery_partner_id', 'delivery_address', 'notes', 'items']

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Order must include at least one item.")
        return value

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        customer_id = validated_data.pop('customer_id', None)
        company_id = validated_data.pop('company_id', None)
        delivery_partner_id = validated_data.pop('delivery_partner_id', None)
        user = self.context['request'].user

        customer_profile = None
        if customer_id:
            from billing.models import Customer
            try:
                customer_profile = Customer.objects.get(id=customer_id)
            except Customer.DoesNotExist:
                raise serializers.ValidationError({'customer_id': 'Customer does not exist.'})

        company = None
        if company_id:
            from billing.models import Company
            try:
                company = Company.objects.get(id=company_id)
            except Company.DoesNotExist:
                raise serializers.ValidationError({'company_id': 'Company does not exist.'})
        elif customer_profile and customer_profile.company:
            company = customer_profile.company

        # If delivery address not provided, default to customer address
        delivery_address = validated_data.get('delivery_address') or (customer_profile.address if customer_profile else '') or 'Walk-in / Stalls'
        validated_data['delivery_address'] = delivery_address

        with transaction.atomic():
            total = Decimal('0.00')
            order = Order.objects.create(
                customer=user,
                customer_profile=customer_profile,
                company=company,
                total_amount=Decimal('0.00'),
                **validated_data
            )

            product_ids = [item['product_id'] for item in items_data]
            products = {p.id: p for p in Product.objects.filter(id__in=product_ids)}

            from billing.invoicing_service import InvoicingService

            pricing_entity = customer_profile or company

            for item_data in items_data:
                pid = item_data['product_id']
                qty = item_data['quantity']
                req_price = item_data.get('requested_price')
                disc_reason = item_data.get('discount_reason', '')

                if pid not in products:
                    raise serializers.ValidationError({'items': f"Product ID {pid} does not exist."})

                product = products[pid]
                standard_rate = InvoicingService.resolve_item_unit_price(pricing_entity, product, qty)

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
            from deliveries.models import Delivery, DeliveryPartner
            driver_user = None
            if delivery_partner_id:
                try:
                    partner = DeliveryPartner.objects.select_related('user').get(id=delivery_partner_id)
                    driver_user = partner.user
                except DeliveryPartner.DoesNotExist:
                    pass

            delivery, _ = Delivery.objects.get_or_create(
                order=order,
                defaults={
                    'driver': driver_user,
                    'status': 'assigned' if driver_user else 'assigned'
                }
            )
            if driver_user and delivery.driver != driver_user:
                delivery.driver = driver_user
                delivery.save(update_fields=['driver'])

            # Trigger FCM notification to Owner
            from notifications.services import NotificationService
            NotificationService.on_order_created(order)
            if driver_user:
                NotificationService.on_delivery_assigned(delivery)

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


class RecurringOrderItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_price = serializers.CharField(source='product.price', read_only=True)

    class Meta:
        model = RecurringOrderItem
        fields = ['id', 'product', 'product_name', 'product_price', 'quantity', 'unit_price']


class RecurringOrderItemWriteSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)


class GlobalRecurringNotificationSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = GlobalRecurringNotificationSetting
        fields = [
            'id', 'order_generation_time', 'owner_notification_time', 'partner_notification_time',
            'owner_notification_enabled', 'partner_notification_enabled', 'timezone', 'updated_at'
        ]
        read_only_fields = ['id', 'updated_at']

    def validate(self, attrs):
        instance = getattr(self, 'instance', None)
        gen_time = attrs.get('order_generation_time', getattr(instance, 'order_generation_time', None))
        owner_time = attrs.get('owner_notification_time', getattr(instance, 'owner_notification_time', None))
        partner_time = attrs.get('partner_notification_time', getattr(instance, 'partner_notification_time', None))

        if gen_time and owner_time and owner_time < gen_time:
            raise serializers.ValidationError({
                'owner_notification_time': 'Owner notification time cannot be earlier than order generation time.'
            })

        if gen_time and partner_time and partner_time < gen_time:
            raise serializers.ValidationError({
                'partner_notification_time': 'Delivery partner notification time cannot be earlier than order generation time.'
            })

        tz_val = attrs.get('timezone', getattr(instance, 'timezone', None))
        if tz_val:
            import zoneinfo
            try:
                zoneinfo.ZoneInfo(tz_val)
            except Exception:
                raise serializers.ValidationError({'timezone': f"Invalid timezone '{tz_val}'."})

        return attrs


class RecurringOrderSerializer(serializers.ModelSerializer):
    items = RecurringOrderItemSerializer(many=True, read_only=True)
    customer_name = serializers.SerializerMethodField()
    customer_phone = serializers.SerializerMethodField()
    delivery_partner_name = serializers.SerializerMethodField()
    is_scheduled_today = serializers.SerializerMethodField()
    today_order_generated = serializers.SerializerMethodField()
    today_order_id = serializers.SerializerMethodField()
    effective_notification_schedule = serializers.SerializerMethodField()

    class Meta:
        model = RecurringOrder
        fields = [
            'id', 'customer', 'customer_profile', 'customer_name', 'customer_phone',
            'company', 'delivery_partner', 'delivery_partner_name',
            'delivery_address', 'frequency', 'weekdays', 'is_paused', 'skipped_dates',
            'auto_approve', 'notes', 'created_at', 'updated_at', 'items',
            'is_scheduled_today', 'today_order_generated', 'today_order_id',
            'use_global_notification_settings', 'custom_order_generation_time',
            'custom_owner_notification_time', 'custom_partner_notification_time',
            'owner_notification_enabled', 'partner_notification_enabled',
            'effective_notification_schedule'
        ]
        read_only_fields = ['id', 'customer', 'created_at', 'updated_at', 'effective_notification_schedule']

    def get_effective_notification_schedule(self, obj):
        global_settings = GlobalRecurringNotificationSetting.get_settings()
        if obj.use_global_notification_settings:
            return {
                'is_custom': False,
                'order_generation_time': str(global_settings.order_generation_time),
                'owner_notification_time': str(global_settings.owner_notification_time),
                'partner_notification_time': str(global_settings.partner_notification_time),
                'owner_notification_enabled': global_settings.owner_notification_enabled,
                'partner_notification_enabled': global_settings.partner_notification_enabled,
                'timezone': global_settings.timezone,
            }
        else:
            return {
                'is_custom': True,
                'order_generation_time': str(obj.custom_order_generation_time) if obj.custom_order_generation_time else str(global_settings.order_generation_time),
                'owner_notification_time': str(obj.custom_owner_notification_time) if obj.custom_owner_notification_time else str(global_settings.owner_notification_time),
                'partner_notification_time': str(obj.custom_partner_notification_time) if obj.custom_partner_notification_time else str(global_settings.partner_notification_time),
                'owner_notification_enabled': obj.owner_notification_enabled,
                'partner_notification_enabled': obj.partner_notification_enabled,
                'timezone': global_settings.timezone,
            }

    def get_customer_name(self, obj):
        if obj.customer_profile:
            return obj.customer_profile.name
        if obj.company:
            return obj.company.name
        return obj.customer.get_full_name() or obj.customer.username

    def get_customer_phone(self, obj):
        if obj.customer_profile:
            return obj.customer_profile.phone
        if obj.company:
            return obj.company.phone
        return getattr(getattr(obj.customer, 'profile', None), 'phone', '')

    def get_delivery_partner_name(self, obj):
        return obj.delivery_partner.name if obj.delivery_partner else None

    def get_is_scheduled_today(self, obj):
        return obj.is_scheduled_for_date(timezone.now().date())

    def get_today_order_generated(self, obj):
        today = timezone.now().date()
        return obj.executions.filter(execution_date=today).exists()

    def get_today_order_id(self, obj):
        today = timezone.now().date()
        exec_obj = obj.executions.filter(execution_date=today).first()
        return exec_obj.order_id if exec_obj else None


class RecurringOrderCreateUpdateSerializer(serializers.ModelSerializer):
    customer_id = serializers.IntegerField(required=False, allow_null=True)
    delivery_partner_id = serializers.IntegerField(required=False, allow_null=True)
    items = RecurringOrderItemWriteSerializer(many=True, write_only=True)

    class Meta:
        model = RecurringOrder
        fields = [
            'id', 'customer_id', 'delivery_partner_id', 'delivery_address',
            'frequency', 'weekdays', 'is_paused', 'auto_approve', 'notes', 'items',
            'use_global_notification_settings', 'custom_order_generation_time',
            'custom_owner_notification_time', 'custom_partner_notification_time',
            'owner_notification_enabled', 'partner_notification_enabled'
        ]

    def validate(self, attrs):
        use_global = attrs.get(
            'use_global_notification_settings',
            getattr(self.instance, 'use_global_notification_settings', True) if self.instance else True
        )
        if not use_global:
            gen_time = attrs.get(
                'custom_order_generation_time',
                getattr(self.instance, 'custom_order_generation_time', None) if self.instance else None
            )
            owner_time = attrs.get(
                'custom_owner_notification_time',
                getattr(self.instance, 'custom_owner_notification_time', None) if self.instance else None
            )
            partner_time = attrs.get(
                'custom_partner_notification_time',
                getattr(self.instance, 'custom_partner_notification_time', None) if self.instance else None
            )

            if not gen_time:
                raise serializers.ValidationError({
                    'custom_order_generation_time': 'Order generation time is required when custom notification schedule is enabled.'
                })
            if not owner_time:
                raise serializers.ValidationError({
                    'custom_owner_notification_time': 'Owner notification time is required when custom notification schedule is enabled.'
                })
            if not partner_time:
                raise serializers.ValidationError({
                    'custom_partner_notification_time': 'Delivery partner notification time is required when custom notification schedule is enabled.'
                })

            if gen_time and owner_time and owner_time < gen_time:
                raise serializers.ValidationError({
                    'custom_owner_notification_time': 'Owner notification time cannot be earlier than order generation time.'
                })
            if gen_time and partner_time and partner_time < gen_time:
                raise serializers.ValidationError({
                    'custom_partner_notification_time': 'Delivery partner notification time cannot be earlier than order generation time.'
                })
        return attrs

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Recurring order must have at least one product item.")
        return value

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        customer_id = validated_data.pop('customer_id', None)
        partner_id = validated_data.pop('delivery_partner_id', None)
        request_user = self.context['request'].user

        customer_profile = None
        target_user = request_user
        if customer_id:
            from billing.models import Customer
            try:
                customer_profile = Customer.objects.get(id=customer_id)
            except Customer.DoesNotExist:
                raise serializers.ValidationError({'customer_id': 'Customer does not exist.'})
            # Link to customer's user account if present, else owner
            from accounts.models import UserProfile
            if customer_profile.phone:
                p_user = UserProfile.objects.filter(phone=customer_profile.phone).first()
                if p_user:
                    target_user = p_user.user

        delivery_partner = None
        if partner_id:
            from deliveries.models import DeliveryPartner
            try:
                delivery_partner = DeliveryPartner.objects.get(id=partner_id)
            except DeliveryPartner.DoesNotExist:
                raise serializers.ValidationError({'delivery_partner_id': 'Delivery partner does not exist.'})

        delivery_address = validated_data.get('delivery_address') or (customer_profile.address if customer_profile else '') or 'Walk-in / Stalls'
        validated_data['delivery_address'] = delivery_address

        with transaction.atomic():
            rec_order = RecurringOrder.objects.create(
                customer=target_user,
                customer_profile=customer_profile,
                company=customer_profile.company if customer_profile else None,
                delivery_partner=delivery_partner,
                **validated_data
            )

            product_ids = [item['product_id'] for item in items_data]
            products = {p.id: p for p in Product.objects.filter(id__in=product_ids)}

            for item in items_data:
                pid = item['product_id']
                if pid not in products:
                    raise serializers.ValidationError({'items': f"Product ID {pid} does not exist."})
                prod = products[pid]
                item_unit_price = item.get('unit_price')
                if item_unit_price is None or item_unit_price <= 0:
                    item_unit_price = prod.price or Decimal('0.00')

                RecurringOrderItem.objects.create(
                    recurring_order=rec_order,
                    product=prod,
                    quantity=item['quantity'],
                    unit_price=item_unit_price
                )

        return rec_order

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        partner_id = validated_data.pop('delivery_partner_id', None)
        customer_id = validated_data.pop('customer_id', None)

        if partner_id is not None:
            if partner_id == 0 or partner_id == '' or partner_id is None:
                instance.delivery_partner = None
            else:
                from deliveries.models import DeliveryPartner
                try:
                    instance.delivery_partner = DeliveryPartner.objects.get(id=partner_id)
                except DeliveryPartner.DoesNotExist:
                    raise serializers.ValidationError({'delivery_partner_id': 'Delivery partner does not exist.'})

        if customer_id is not None:
            if customer_id == 0 or customer_id == '':
                instance.customer_profile = None
                instance.company = None
            else:
                from billing.models import Customer
                try:
                    cust_obj = Customer.objects.get(id=customer_id)
                    instance.customer_profile = cust_obj
                    instance.company = cust_obj.company
                    # Link to customer's user account if present, otherwise preserve existing or fallback to request user
                    from accounts.models import UserProfile
                    p_user = None
                    if cust_obj.phone:
                        p_user = UserProfile.objects.filter(phone=cust_obj.phone).first()
                    if p_user and p_user.user:
                        instance.customer = p_user.user
                    elif not instance.customer_id:
                        request_user = self.context.get('request').user if self.context.get('request') else None
                        if request_user and request_user.is_authenticated:
                            instance.customer = request_user
                except Customer.DoesNotExist:
                    raise serializers.ValidationError({'customer_id': 'Customer does not exist.'})

        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()

            # Update items if provided (Future orders only - does not affect historical orders)
            if items_data is not None:
                instance.items.all().delete()
                product_ids = [item['product_id'] for item in items_data]
                products = {p.id: p for p in Product.objects.filter(id__in=product_ids)}
                for item in items_data:
                    pid = item['product_id']
                    if pid not in products:
                        raise serializers.ValidationError({'items': f"Product ID {pid} does not exist."})
                    prod = products[pid]
                    item_unit_price = item.get('unit_price')
                    if item_unit_price is None or item_unit_price <= 0:
                        item_unit_price = prod.price or Decimal('0.00')

                    RecurringOrderItem.objects.create(
                        recurring_order=instance,
                        product=prod,
                        quantity=item['quantity'],
                        unit_price=item_unit_price
                    )

        return instance


# ============================================================================
# APPROVED FEATURE: DAILY DELIVERY SCHEDULE SERIALIZERS
# ============================================================================

class DailyDeliveryScheduleItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_price = serializers.DecimalField(source='product.price', max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = DailyDeliveryScheduleItem
        fields = ['id', 'product', 'product_name', 'product_price', 'quantity', 'unit_price']


class DailyDeliveryScheduleItemWriteSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=Decimal('0.00'))


class ScheduleCancellationExceptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScheduleCancellationException
        fields = ['id', 'cancelled_date', 'reason', 'created_at']


class DailyDeliveryScheduleSerializer(serializers.ModelSerializer):
    items = DailyDeliveryScheduleItemSerializer(many=True, read_only=True)
    cancellation_exceptions = ScheduleCancellationExceptionSerializer(many=True, read_only=True)
    customer_name = serializers.CharField(source='customer_profile.name', read_only=True)
    customer_phone = serializers.CharField(source='customer_profile.phone', read_only=True)
    delivery_partner_name = serializers.CharField(source='delivery_partner.name', read_only=True)
    delivery_partner_phone = serializers.CharField(source='delivery_partner.phone', read_only=True)
    next_scheduled_delivery = serializers.SerializerMethodField()

    class Meta:
        model = DailyDeliverySchedule
        fields = [
            'id', 'customer_profile', 'customer_name', 'customer_phone',
            'delivery_partner', 'delivery_partner_name', 'delivery_partner_phone',
            'delivery_time', 'notification_lead_time_minutes', 'weekdays',
            'delivery_address', 'start_date', 'end_date', 'is_active', 'notes',
            'created_at', 'updated_at', 'items', 'cancellation_exceptions',
            'next_scheduled_delivery'
        ]

    def get_next_scheduled_delivery(self, obj) -> str:
        """Finds next eligible delivery date within next 14 days."""
        from datetime import date, timedelta
        from django.utils import timezone
        import zoneinfo
        tz = zoneinfo.ZoneInfo('Asia/Kolkata')
        today = timezone.now().astimezone(tz).date()

        for offset in range(0, 15):
            candidate = today + timedelta(days=offset)
            if obj.is_eligible_for_date(candidate):
                return candidate.isoformat()
        return None


class DailyDeliveryScheduleWriteSerializer(serializers.ModelSerializer):
    customer_id = serializers.IntegerField(write_only=True)
    delivery_partner_id = serializers.IntegerField(write_only=True)
    items = DailyDeliveryScheduleItemWriteSerializer(many=True, write_only=True)

    class Meta:
        model = DailyDeliverySchedule
        fields = [
            'id', 'customer_id', 'delivery_partner_id', 'delivery_time',
            'notification_lead_time_minutes', 'weekdays', 'delivery_address',
            'start_date', 'end_date', 'is_active', 'notes', 'items'
        ]

    def validate(self, attrs):
        from billing.models import Customer
        from deliveries.models import DeliveryPartner

        customer_id = attrs.get('customer_id')
        if customer_id:
            try:
                cust = Customer.objects.select_related('user').get(id=customer_id)
                self.context['validated_customer'] = cust
            except Customer.DoesNotExist:
                raise serializers.ValidationError({'customer_id': 'Customer profile does not exist.'})

        partner_id = attrs.get('delivery_partner_id')
        if partner_id:
            try:
                partner = DeliveryPartner.objects.get(id=partner_id)
                self.context['validated_partner'] = partner
            except DeliveryPartner.DoesNotExist:
                raise serializers.ValidationError({'delivery_partner_id': 'Delivery partner does not exist.'})

        items = attrs.get('items', [])
        if not items and self.instance is None:
            raise serializers.ValidationError({'items': 'At least one product is required.'})

        return attrs

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        validated_data.pop('customer_id', None)
        validated_data.pop('delivery_partner_id', None)

        customer = self.context['validated_customer']
        partner = self.context['validated_partner']

        with transaction.atomic():
            schedule = DailyDeliverySchedule.objects.create(
                customer_profile=customer,
                customer_user=customer.user or self.context['request'].user,
                delivery_partner=partner,
                delivery_address=validated_data.get('delivery_address') or customer.address or '',
                **validated_data
            )

            product_ids = [it['product_id'] for it in items_data]
            products = {p.id: p for p in Product.objects.filter(id__in=product_ids)}

            for it in items_data:
                pid = it['product_id']
                if pid not in products:
                    raise serializers.ValidationError({'items': f"Product {pid} does not exist."})
                prod = products[pid]
                uprice = it.get('unit_price') or prod.price or Decimal('0.00')
                DailyDeliveryScheduleItem.objects.create(
                    schedule=schedule,
                    product=prod,
                    quantity=it['quantity'],
                    unit_price=uprice
                )

        return schedule

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        validated_data.pop('customer_id', None)
        partner_id = validated_data.pop('delivery_partner_id', None)

        if partner_id and 'validated_partner' in self.context:
            instance.delivery_partner = self.context['validated_partner']

        with transaction.atomic():
            for attr, val in validated_data.items():
                setattr(instance, attr, val)
            instance.save()

            if items_data is not None:
                instance.items.all().delete()
                product_ids = [it['product_id'] for it in items_data]
                products = {p.id: p for p in Product.objects.filter(id__in=product_ids)}
                for it in items_data:
                    pid = it['product_id']
                    if pid not in products:
                        raise serializers.ValidationError({'items': f"Product {pid} does not exist."})
                    prod = products[pid]
                    uprice = it.get('unit_price') or prod.price or Decimal('0.00')
                    DailyDeliveryScheduleItem.objects.create(
                        schedule=instance,
                        product=prod,
                        quantity=it['quantity'],
                        unit_price=uprice
                    )

        return instance

