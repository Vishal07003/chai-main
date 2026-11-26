from decimal import Decimal
import calendar
from datetime import date, datetime, time
from django.db import transaction, models
from django.utils import timezone
from rest_framework.exceptions import ValidationError, PermissionDenied
from .models import Invoice, InvoiceItem, InvoiceOrder, Customer, Company, Payment
from orders.models import Order, OrderItem
from products.models import Product


class MonthlyBillingService:
    """
    Centralized Domain Service for Monthly Billing System:
    - Generates one monthly consolidated invoice per customer per month.
    - Groups eligible delivered orders from Quick Billing and Daily Schedule.
    - Preserves individual order links via InvoiceOrder.
    - Prevents duplicate monthly invoices for the same customer and month.
    - Retries safely without duplicate items or double-counting.
    - Strictly preserves existing historical invoices and transactions.
    """

    @classmethod
    def clamp_day_to_month(cls, year: int, month: int, day: int) -> int:
        """
        Clamp day of month to maximum calendar days for given year and month.
        E.g., day 31 in April (30 days) -> 30; day 31 in Feb 2024 -> 29; day 31 in Feb 2023 -> 28.
        """
        _, max_days = calendar.monthrange(year, month)
        return min(max(1, day), max_days)

    @classmethod
    def get_customer_billing_period(cls, customer_or_cycle_day, year: int, month: int):
        """
        Calculate customer-specific billing period based on customer's billing_cycle_day.
        Returns:
            start_date: inclusive start date (e.g. Nov 10, 2026)
            end_date: inclusive end date (e.g. Dec 9, 2026)
            start_dt: timezone-aware start datetime (min time on start_date)
            end_dt: timezone-aware end datetime (max time on end_date)
            period_str: formatted period identifier 'YYYY-MM'

        Cycle day behavior:
        - If cycle_day is 1: starts on 1st of month, ends on last day of same month.
        - If cycle_day is N (2..31):
          Cycle for (year, month) starts on day N of (year, month) [clamped to last day of that month]
          and ends on the day before day N of the next month [clamped to last day of next month].
          Example: Day 10 for (2026, 11): Nov 10, 2026 to Dec 9, 2026.
          Example: Day 31 for (2026, 1): Jan 31, 2026 to Feb 27, 2026 (or Feb 28 in leap year).
          Example: Day 30 for (2026, 1): Jan 30, 2026 to Feb 27, 2026.
        """
        if isinstance(customer_or_cycle_day, (int, str)):
            cycle_day = int(customer_or_cycle_day)
        else:
            cycle_day = getattr(customer_or_cycle_day, 'billing_cycle_day', 1) or 1

        cycle_day = max(1, min(31, cycle_day))
        period_str = f"{year:04d}-{month:02d}"

        if cycle_day == 1:
            _, last_day = calendar.monthrange(year, month)
            start_date = date(year, month, 1)
            end_date = date(year, month, last_day)
        else:
            start_day = cls.clamp_day_to_month(year, month, cycle_day)
            start_date = date(year, month, start_day)

            # Next month calculation
            if month == 12:
                next_year = year + 1
                next_month = 1
            else:
                next_year = year
                next_month = month + 1

            next_start_day = cls.clamp_day_to_month(next_year, next_month, cycle_day)
            next_start_date = date(next_year, next_month, next_start_day)
            from datetime import timedelta
            end_date = next_start_date - timedelta(days=1)

        start_dt = timezone.make_aware(datetime.combine(start_date, time.min))
        end_dt = timezone.make_aware(datetime.combine(end_date, time.max))
        return start_date, end_date, start_dt, end_dt

    @classmethod
    def get_month_date_range(cls, year: int, month: int):
        """
        Standard calendar month date range (1st to last calendar day).
        Maintained for backwards-compatibility.
        """
        return cls.get_customer_billing_period(1, year, month)

    @classmethod
    def get_eligible_unbilled_orders(cls, customer: Customer, year: int, month: int):
        """
        Find all delivered orders for the customer within the customer's billing cycle period
        for the given year and month that have NOT yet been invoiced.
        Includes orders from both Daily Schedule and Quick Billing / One-Time.
        """
        start_date, end_date, start_dt, end_dt = cls.get_customer_billing_period(customer, year, month)

        # Delivered orders matching customer_profile or customer_user linked to this profile
        q_customer = models.Q(customer_profile=customer)
        if hasattr(customer, 'company') and customer.company:
            q_customer |= models.Q(company=customer.company)

        # Date matching scheduled_delivery_date in month OR created_at in month if no scheduled_delivery_date
        q_date = (
            (models.Q(scheduled_delivery_date__isnull=False) & models.Q(scheduled_delivery_date__range=(start_date, end_date))) |
            (models.Q(scheduled_delivery_date__isnull=True) & models.Q(created_at__range=(start_dt, end_dt)))
        )

        orders = (
            Order.objects.filter(q_customer)
            .filter(q_date)
            .filter(status='delivered')
            .filter(invoice__isnull=True)  # No direct 1:1 invoice
            .filter(order_billing__isnull=True)  # Not in any consolidated invoice
            .prefetch_related('items__product', 'delivery__delivered_items')
            .order_by('scheduled_delivery_date', 'created_at', 'id')
        )

        return list(orders)

    @classmethod
    def generate_monthly_invoices_for_month(cls, year: int, month: int, owner_user, customer_ids: list = None) -> list:
        """
        Generate monthly invoices for all customers (or selected customer_ids)
        for the given year and month.
        Safe to retry: Skips customers who already have a monthly invoice for that period.
        """
        if not owner_user.is_staff and not owner_user.is_superuser:
            if not (hasattr(owner_user, 'profile') and owner_user.profile.role == 'owner'):
                raise PermissionDenied("Only the business owner can generate monthly bills.")

        billing_period = f"{year:04d}-{month:02d}"

        # Fetch eligible customers
        cust_qs = Customer.objects.all()
        if customer_ids:
            cust_qs = cust_qs.filter(id__in=customer_ids)

        generated_invoices = []

        for customer in cust_qs:
            # Check if monthly invoice already exists for this period
            existing = Invoice.objects.filter(
                customer=customer,
                billing_period=billing_period,
                billing_type='monthly',
            ).exclude(status='void').first()

            if existing:
                # Already generated for this customer & month; skip to guarantee idempotency
                continue

            inv = cls.generate_customer_monthly_invoice(
                customer=customer,
                year=year,
                month=month,
                owner_user=owner_user,
            )
            if inv:
                generated_invoices.append(inv)

        return generated_invoices

    @classmethod
    def generate_customer_monthly_invoice(cls, customer: Customer, year: int, month: int, owner_user, notes: str = '') -> Invoice:
        """
        Generate one monthly consolidated invoice for a single customer atomically.
        If no eligible unbilled orders exist, returns None.
        """
        billing_period = f"{year:04d}-{month:02d}"

        with transaction.atomic():
            # Check duplicate under lock
            existing = Invoice.objects.select_for_update().filter(
                customer=customer,
                billing_period=billing_period,
                billing_type='monthly',
            ).exclude(status='void').first()

            if existing:
                raise ValidationError({
                    'detail': f"Monthly invoice {existing.invoice_number} already exists for {customer.name} for {billing_period}."
                })

            eligible_orders = cls.get_eligible_unbilled_orders(customer, year, month)
            if not eligible_orders:
                return None

            # Lock eligible order rows to prevent double billing
            order_ids = [o.id for o in eligible_orders]
            locked_orders = list(Order.objects.select_for_update().filter(id__in=order_ids))

            # Re-verify none were invoiced while waiting for lock
            for o in locked_orders:
                if hasattr(o, 'invoice') and o.invoice:
                    raise ValidationError({'detail': f"Order #{o.id} already has invoice {o.invoice.invoice_number}."})
                if hasattr(o, 'order_billing') and o.order_billing:
                    raise ValidationError({'detail': f"Order #{o.id} already attached to invoice {o.order_billing.invoice.invoice_number}."})

            # Generate monthly invoice number: e.g. INV-YYYYMM-CUSTID-XXXX
            today_str = timezone.now().strftime('%Y%m%d')
            today_invoices = Invoice.objects.filter(invoice_number__startswith=f"INV-{today_str}-")
            max_num = 0
            for ti in today_invoices:
                try:
                    num_part = int(ti.invoice_number.split('-')[-1])
                    if num_part > max_num:
                        max_num = num_part
                except (ValueError, IndexError):
                    pass
            invoice_number = f"INV-{today_str}-{(max_num + 1):04d}"

            company = customer.company
            customer_name = customer.name
            customer_phone = customer.phone or (company.phone if company else '')

            month_name = calendar.month_name[month]
            default_notes = notes or f"Monthly Bill for {month_name} {year} ({len(locked_orders)} deliveries)"

            invoice = Invoice.objects.create(
                invoice_number=invoice_number,
                billing_type='monthly',
                billing_period=billing_period,
                company=company,
                customer=customer,
                customer_name=customer_name,
                customer_phone=customer_phone,
                status='unpaid',
                finalized_at=timezone.now(),
                notes=default_notes,
                generated_by=owner_user,
            )

            # Link orders via InvoiceOrder
            for o in locked_orders:
                InvoiceOrder.objects.create(
                    invoice=invoice,
                    order=o,
                )

            # Aggregate products across all orders
            # Product key: (product_id, unit_price, product_name)
            product_aggregates = {}

            for o in locked_orders:
                delivery = getattr(o, 'delivery', None)
                delivered_qty_map = {}
                if delivery:
                    for di in delivery.delivered_items.all():
                        if di.product_id:
                            delivered_qty_map[di.product_id] = di.delivered_quantity

                for item in o.items.all():
                    pid = item.product_id
                    p_name = item.product.name if item.product else f"Product {pid}"
                    u_price = item.price
                    actual_qty = delivered_qty_map.get(pid, item.quantity)
                    if actual_qty <= 0:
                        continue

                    key = (pid, u_price, p_name)
                    if key not in product_aggregates:
                        product_aggregates[key] = {
                            'product': item.product,
                            'quantity': 0,
                            'unit_price': u_price,
                            'product_name': p_name,
                        }
                    product_aggregates[key]['quantity'] += actual_qty

            subtotal = Decimal('0.00')
            for key, p_data in product_aggregates.items():
                qty = p_data['quantity']
                rate = p_data['unit_price']
                line_total = rate * qty
                subtotal += line_total

                prod_obj = p_data['product']
                unit_cost_val = getattr(prod_obj, 'cost_price', None) if prod_obj else None
                total_cost_val = (unit_cost_val * qty) if unit_cost_val is not None else None

                InvoiceItem.objects.create(
                    invoice=invoice,
                    product=prod_obj,
                    product_name=p_data['product_name'],
                    unit_price=rate,
                    quantity=qty,
                    total_price=line_total,
                    unit_cost=unit_cost_val,
                    total_cost=total_cost_val,
                )

            invoice.subtotal = subtotal
            invoice.discount_amount = Decimal('0.00')
            invoice.grand_total = subtotal
            invoice.save(update_fields=['subtotal', 'discount_amount', 'grand_total'])

            return invoice
