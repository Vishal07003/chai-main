from django.contrib import admin
from .models import Order, OrderItem, RecurringOrder, RecurringOrderItem, RecurringOrderExecution

class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 1

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'customer_profile', 'status', 'total_amount', 'created_at')
    list_filter = ('status', 'created_at')
    inlines = [OrderItemInline]

class RecurringOrderItemInline(admin.TabularInline):
    model = RecurringOrderItem
    extra = 1

@admin.register(RecurringOrder)
class RecurringOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer_profile', 'frequency', 'delivery_partner', 'is_paused', 'created_at')
    list_filter = ('frequency', 'is_paused', 'created_at')
    inlines = [RecurringOrderItemInline]

@admin.register(RecurringOrderExecution)
class RecurringOrderExecutionAdmin(admin.ModelAdmin):
    list_display = ('id', 'recurring_order', 'order', 'execution_date', 'created_at')
    list_filter = ('execution_date',)

admin.site.register(OrderItem)
admin.site.register(RecurringOrderItem)
