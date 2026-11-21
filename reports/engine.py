from decimal import Decimal
import datetime
from django.db.models import Sum, Count, F, Q, Case, When, Value, DecimalField, IntegerField
from django.db.models.functions import TruncDate
from django.utils import timezone
from zoneinfo import ZoneInfo

from billing.models import Invoice, InvoiceItem, InvoiceOrder, Payment, PaymentAllocation, Customer, Expense
from deliveries.models import Delivery, DeliveryItem, DeliveryPartner
from .utils import KOLKATA_TZ


class ReportsEngine:
    """
    Centralized High-Performance Domain Engine for all 8 Chai Flow Reports:
    - Pure database-level aggregations
    - Strict accounting isolation (Finalized invoices only, Drafts excluded)
    - Advance payments accounted under collections, excluded from sales
    - Overdue balances strictly requiring explicit due_date < today
    - Cost of Goods Sold (COGS) with explicit coverage status (complete, partial, unavailable)
    """

    @classmethod
    def get_overview(cls, range_info: dict) -> dict:
        """
        Executive KPI Summary Deck:
        - Net Sales
        - Collections Received
        - Total Outstanding (current portfolio snapshot)
        - Operating Expenses
        - Gross & Net Profit estimates
        """
        sales = cls.get_sales_report(range_info)['summary']
        collections = cls.get_collections_report(range_info)['summary']
        outstanding = cls.get_outstanding_report(range_info)['summary']
        pnl = cls.get_profit_loss_report(range_info)['summary']

        return {
            'date_filter': range_info,
            'net_sales': sales['net_sales'],
            'invoices_count': sales['invoice_count'],
            'total_collections': collections['total_collections'],
            'total_outstanding': outstanding['total_outstanding'],
            'overdue_outstanding': outstanding['overdue_outstanding'],
            'defaulters_count': outstanding['overdue_invoices_count'],
            'gross_profit': pnl['gross_profit'],
            'operating_expenses': pnl['operating_expenses'],
            'net_profit': pnl['net_profit'],
            'profit_margin_pct': pnl['profit_margin_pct'],
            'cost_coverage_status': pnl['cost_coverage_status'],
        }

    @classmethod
    def get_sales_report(cls, range_info: dict) -> dict:
        """
        Sales Report:
        - Source: Finalized non-void Invoice records created in date range
        - Excludes all DraftInvoice
        - Breakdown by Quick Billing, Single Order, Consolidated Billing
        - Time series trend
        """
        start_dt = range_info['start_dt']
        end_dt = range_info['end_dt']

        invoices_qs = Invoice.objects.filter(
            created_at__gte=start_dt,
            created_at__lte=end_dt,
        ).exclude(status='void')

        agg = invoices_qs.aggregate(
            gross_subtotal=Sum('subtotal'),
            total_discounts=Sum('discount_amount'),
            net_sales=Sum('grand_total'),
            invoice_count=Count('id'),
        )

        gross_subtotal = agg['gross_subtotal'] or Decimal('0.00')
        total_discounts = agg['total_discounts'] or Decimal('0.00')
        net_sales = agg['net_sales'] or Decimal('0.00')
        invoice_count = agg['invoice_count'] or 0

        # Breakdown by billing mode
        # Consolidated: invoice_orders count > 0
        # Single order: order is not null and not in invoice_orders
        # Quick billing: order is null and invoice_orders count == 0
        annotated_invoices = invoices_qs.annotate(
            cons_count=Count('invoice_orders')
        )

        quick_billing_invoices = annotated_invoices.filter(order__isnull=True, cons_count=0)
        single_order_invoices = annotated_invoices.filter(order__isnull=False, cons_count__lte=1)
        consolidated_invoices = annotated_invoices.filter(cons_count__gt=1)

        qb_sales = quick_billing_invoices.aggregate(s=Sum('grand_total'), c=Count('id'))
        so_sales = single_order_invoices.aggregate(s=Sum('grand_total'), c=Count('id'))
        cons_sales = consolidated_invoices.aggregate(s=Sum('grand_total'), c=Count('id'))

        # Daily Trend Grouping (in Asia/Kolkata)
        trend_qs = invoices_qs.annotate(
            day=TruncDate('created_at', tzinfo=KOLKATA_TZ)
        ).values('day').annotate(
            sales=Sum('grand_total'),
            count=Count('id')
        ).order_by('day')

        trend = [
            {
                'date': item['day'].isoformat(),
                'sales': str(item['sales'] or Decimal('0.00')),
                'invoice_count': item['count'],
            }
            for item in trend_qs
        ]

        # Detailed Invoice List
        detailed_invoices = [
            {
                'id': inv.id,
                'invoice_number': inv.invoice_number,
                'customer_name': inv.customer_name,
                'customer_phone': inv.customer_phone,
                'subtotal': str(inv.subtotal),
                'discount_amount': str(inv.discount_amount),
                'grand_total': str(inv.grand_total),
                'status': inv.status,
                'due_date': inv.due_date.isoformat() if inv.due_date else None,
                'created_at': inv.created_at.astimezone(KOLKATA_TZ).strftime('%Y-%m-%d %H:%M'),
                'source': 'Consolidated' if getattr(inv, 'invoice_orders', None) and inv.invoice_orders.count() > 1 else ('Single Order' if inv.order_id else 'Quick Billing'),
            }
            for inv in invoices_qs.select_related('customer', 'order').prefetch_related('invoice_orders').order_by('-created_at')[:200]
        ]

        return {
            'date_filter': range_info,
            'summary': {
                'gross_subtotal': str(gross_subtotal),
                'total_discounts': str(total_discounts),
                'net_sales': str(net_sales),
                'invoice_count': invoice_count,
                'quick_billing': {
                    'sales': str(qb_sales['s'] or Decimal('0.00')),
                    'count': qb_sales['c'] or 0,
                },
                'single_order': {
                    'sales': str(so_sales['s'] or Decimal('0.00')),
                    'count': so_sales['c'] or 0,
                },
                'consolidated': {
                    'sales': str(cons_sales['s'] or Decimal('0.00')),
                    'count': cons_sales['c'] or 0,
                },
            },
            'trend': trend,
            'invoices': detailed_invoices,
        }

    @classmethod
    def get_collections_report(cls, range_info: dict) -> dict:
        """
        Collection Report:
        - Source: Payment.objects where received_at in date range
        - Advances counted as collections, never as sales
        - Zero double-counting: aggregations operate strictly on Payment.amount
        """
        start_dt = range_info['start_dt']
        end_dt = range_info['end_dt']

        payments_qs = Payment.objects.filter(
            received_at__gte=start_dt,
            received_at__lte=end_dt,
        )

        agg = payments_qs.aggregate(
            total_collections=Sum('amount'),
            total_count=Count('id')
        )
        total_collections = agg['total_collections'] or Decimal('0.00')
        total_count = agg['total_count'] or 0

        # Method-wise breakdown
        method_qs = payments_qs.values('method').annotate(
            total=Sum('amount'),
            count=Count('id')
        ).order_by('-total')

        methods = {
            m['method']: {
                'total': str(m['total'] or Decimal('0.00')),
                'count': m['count'],
            }
            for m in method_qs
        }

        # Daily Trend Grouping
        trend_qs = payments_qs.annotate(
            day=TruncDate('received_at', tzinfo=KOLKATA_TZ)
        ).values('day').annotate(
            amount=Sum('amount'),
            count=Count('id')
        ).order_by('day')

        trend = [
            {
                'date': item['day'].isoformat(),
                'amount': str(item['amount'] or Decimal('0.00')),
                'count': item['count'],
            }
            for item in trend_qs
        ]

        # Recent Payment records
        payment_list = [
            {
                'id': p.id,
                'customer_name': p.customer.name if p.customer else (p.company.name if p.company else 'Direct Account'),
                'amount': str(p.amount),
                'method': p.get_method_display(),
                'reference_number': p.reference_number,
                'received_at': p.received_at.astimezone(KOLKATA_TZ).strftime('%Y-%m-%d %H:%M'),
                'is_advance': p.unallocated_amount > Decimal('0.00'),
                'unallocated_advance': str(p.unallocated_amount),
            }
            for p in payments_qs.select_related('customer', 'company').order_by('-received_at')[:200]
        ]

        return {
            'date_filter': range_info,
            'summary': {
                'total_collections': str(total_collections),
                'payment_count': total_count,
                'methods': methods,
            },
            'trend': trend,
            'payments': payment_list,
        }

    @classmethod
    def get_outstanding_report(cls, range_info: dict) -> dict:
        """
        Outstanding & Ageing Report:
        - Computes true pending balances on all unpaid/partially paid non-void Invoices
        - Overdue strictly requires: due_date is NOT NULL, due_date < today, and pending_amount > 0
        - Ageing buckets (0-15, 16-30, 31-60, 60+ days)
        - Separately aggregates customer unallocated advance credits
        """
        today = timezone.now().astimezone(KOLKATA_TZ).date()

        invoices_qs = Invoice.objects.exclude(status__in=['paid', 'void']).select_related('customer', 'company').prefetch_related('allocations', 'payments')

        total_outstanding = Decimal('0.00')
        overdue_outstanding = Decimal('0.00')
        overdue_invoices_count = 0

        bucket_0_15 = Decimal('0.00')
        bucket_16_30 = Decimal('0.00')
        bucket_31_60 = Decimal('0.00')
        bucket_60_plus = Decimal('0.00')

        customer_dues = {}
        invoice_rows = []

        for inv in invoices_qs:
            pending = inv.pending_amount
            if pending <= Decimal('0.00'):
                continue

            total_outstanding += pending

            is_overdue = False
            days_overdue = 0
            if inv.due_date is not None and inv.due_date < today:
                is_overdue = True
                days_overdue = (today - inv.due_date).days
                overdue_outstanding += pending
                overdue_invoices_count += 1

            # Ageing bucket based on invoice age (created_at) or due_date age
            age_days = (today - inv.created_at.astimezone(KOLKATA_TZ).date()).days
            if age_days <= 15:
                bucket_0_15 += pending
            elif age_days <= 30:
                bucket_16_30 += pending
            elif age_days <= 60:
                bucket_31_60 += pending
            else:
                bucket_60_plus += pending

            # Customer group
            cust_id = inv.customer_id or f"comp_{inv.company_id}" or "walkin"
            cust_name = inv.customer.name if inv.customer else (inv.company.name if inv.company else inv.customer_name)
            cust_phone = inv.customer.phone if inv.customer else (inv.company.phone if inv.company else inv.customer_phone)

            if cust_id not in customer_dues:
                customer_dues[cust_id] = {
                    'customer_id': inv.customer_id,
                    'customer_name': cust_name,
                    'customer_phone': cust_phone,
                    'total_pending': Decimal('0.00'),
                    'overdue_pending': Decimal('0.00'),
                    'invoices_count': 0,
                    'oldest_invoice_date': inv.created_at.date(),
                }
            customer_dues[cust_id]['total_pending'] += pending
            if is_overdue:
                customer_dues[cust_id]['overdue_pending'] += pending
            customer_dues[cust_id]['invoices_count'] += 1

            invoice_rows.append({
                'id': inv.id,
                'invoice_number': inv.invoice_number,
                'customer_name': cust_name,
                'customer_phone': cust_phone,
                'grand_total': str(inv.grand_total),
                'total_paid': str(inv.total_paid),
                'pending_amount': str(pending),
                'status': inv.status,
                'due_date': inv.due_date.isoformat() if inv.due_date else None,
                'is_overdue': is_overdue,
                'days_overdue': days_overdue,
                'created_at': inv.created_at.astimezone(KOLKATA_TZ).strftime('%Y-%m-%d'),
            })

        # Calculate total unallocated customer credit (advances)
        all_payments = Payment.objects.all().prefetch_related('allocations')
        total_advance_credit = Decimal('0.00')
        for p in all_payments:
            total_advance_credit += p.unallocated_amount

        # Sort customer dues by highest pending
        sorted_customers = sorted(
            [
                {
                    **c,
                    'total_pending': str(c['total_pending']),
                    'overdue_pending': str(c['overdue_pending']),
                    'oldest_invoice_date': c['oldest_invoice_date'].isoformat(),
                }
                for c in customer_dues.values()
            ],
            key=lambda x: Decimal(x['total_pending']),
            reverse=True
        )

        return {
            'date_filter': range_info,
            'summary': {
                'total_outstanding': str(total_outstanding),
                'overdue_outstanding': str(overdue_outstanding),
                'overdue_invoices_count': overdue_invoices_count,
                'total_advance_credits': str(total_advance_credit),
                'ageing_buckets': {
                    '0_15_days': str(bucket_0_15),
                    '16_30_days': str(bucket_16_30),
                    '31_60_days': str(bucket_31_60),
                    '60_plus_days': str(bucket_60_plus),
                },
            },
            'customer_dues': sorted_customers[:100],
            'invoices': invoice_rows[:200],
        }

    @classmethod
    def get_products_report(cls, range_info: dict) -> dict:
        """
        Product Sales & Velocity Report:
        - Source: InvoiceItem rows belonging to finalized non-void Invoices within date range
        - Quantity sold, sales value, cost coverage indicator
        """
        start_dt = range_info['start_dt']
        end_dt = range_info['end_dt']

        items_qs = InvoiceItem.objects.filter(
            invoice__created_at__gte=start_dt,
            invoice__created_at__lte=end_dt,
        ).exclude(invoice__status='void')

        agg = items_qs.aggregate(
            total_qty_sold=Sum('quantity'),
            total_sales_value=Sum('total_price'),
            total_cost_captured=Sum('total_cost'),
            items_count=Count('id')
        )

        total_qty_sold = agg['total_qty_sold'] or 0
        total_sales_value = agg['total_sales_value'] or Decimal('0.00')

        # Product-wise aggregation
        prod_qs = items_qs.values('product_id', 'product_name').annotate(
            units_sold=Sum('quantity'),
            sales_value=Sum('total_price'),
            cost_value=Sum('total_cost'),
            lines_count=Count('id')
        ).order_by('-sales_value')

        product_list = []
        for p in prod_qs:
            val = p['sales_value'] or Decimal('0.00')
            cost = p['cost_value']
            has_cost = cost is not None
            gross_margin = (val - cost) if has_cost else None
            margin_pct = (round((gross_margin / val) * 100, 1)) if (has_cost and val > 0) else None

            product_list.append({
                'product_id': p['product_id'],
                'product_name': p['product_name'],
                'units_sold': p['units_sold'],
                'sales_value': str(val),
                'cost_value': str(cost) if has_cost else None,
                'gross_margin': str(gross_margin) if gross_margin is not None else None,
                'margin_pct': margin_pct,
                'cost_available': has_cost,
            })

        return {
            'date_filter': range_info,
            'summary': {
                'total_qty_sold': total_qty_sold,
                'total_sales_value': str(total_sales_value),
                'distinct_products_sold': len(product_list),
            },
            'products': product_list,
        }

    @classmethod
    def get_customers_report(cls, range_info: dict) -> dict:
        """
        Customer Sales, Collections & Outstanding Report:
        - Customer-wise sales generated within the date range
        - Collections recorded from each customer within the date range
        - Current ledger pending balance
        """
        start_dt = range_info['start_dt']
        end_dt = range_info['end_dt']

        customers = Customer.objects.all().prefetch_related('invoices', 'payments')

        customer_list = []
        for c in customers:
            # Range sales
            range_invoices = c.invoices.filter(created_at__gte=start_dt, created_at__lte=end_dt).exclude(status='void')
            sales_val = range_invoices.aggregate(s=Sum('grand_total'))['s'] or Decimal('0.00')
            inv_count = range_invoices.count()

            # Range collections
            range_payments = c.payments.filter(received_at__gte=start_dt, received_at__lte=end_dt)
            coll_val = range_payments.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

            # Portfolio pending
            all_invoices = c.invoices.exclude(status='void')
            total_pending = sum(inv.pending_amount for inv in all_invoices)
            last_inv = all_invoices.order_by('-created_at').first()

            if sales_val > Decimal('0.00') or coll_val > Decimal('0.00') or total_pending > Decimal('0.00'):
                customer_list.append({
                    'customer_id': c.id,
                    'customer_name': c.name,
                    'customer_phone': c.phone,
                    'address': c.address,
                    'period_sales': str(sales_val),
                    'period_collections': str(coll_val),
                    'current_outstanding': str(total_pending),
                    'period_invoices_count': inv_count,
                    'last_purchase_date': last_inv.created_at.astimezone(KOLKATA_TZ).strftime('%Y-%m-%d') if last_inv else None,
                })

        # Sort by period sales descending
        customer_list.sort(key=lambda x: Decimal(x['period_sales']), reverse=True)

        return {
            'date_filter': range_info,
            'summary': {
                'active_customers_count': len(customer_list),
                'top_customer': customer_list[0]['customer_name'] if customer_list else None,
            },
            'customers': customer_list[:100],
        }

    @classmethod
    def get_deliveries_report(cls, range_info: dict) -> dict:
        """
        Delivery & Partner Performance Report:
        - Source: Delivery.objects within date range
        - Fulfillment statuses: assigned, accepted, picked_up, on_the_way, delivered, failed
        - Ordered vs delivered quantity audit variance
        - Partner volume and completion rate
        """
        start_dt = range_info['start_dt']
        end_dt = range_info['end_dt']

        deliveries_qs = Delivery.objects.filter(
            order__created_at__gte=start_dt,
            order__created_at__lte=end_dt,
        ).select_related('order', 'driver', 'driver__partner_profile').prefetch_related('delivered_items')

        total_deliveries = deliveries_qs.count()
        delivered_count = deliveries_qs.filter(status='delivered').count()
        failed_count = deliveries_qs.filter(status='failed').count()
        in_progress_count = total_deliveries - (delivered_count + failed_count)

        completion_rate = round((delivered_count / total_deliveries * 100), 1) if total_deliveries > 0 else 0.0

        # Partner-wise performance
        partner_stats = {}
        discrepancy_logs = []

        for d in deliveries_qs:
            driver_name = d.driver.partner_profile.name if hasattr(d.driver, 'partner_profile') and d.driver.partner_profile else (d.driver.username if d.driver else 'Unassigned')
            driver_id = d.driver_id or 0

            if driver_id not in partner_stats:
                partner_stats[driver_id] = {
                    'partner_name': driver_name,
                    'total_assigned': 0,
                    'delivered': 0,
                    'failed': 0,
                }
            partner_stats[driver_id]['total_assigned'] += 1
            if d.status == 'delivered':
                partner_stats[driver_id]['delivered'] += 1
            elif d.status == 'failed':
                partner_stats[driver_id]['failed'] += 1

            # Audit quantity discrepancies
            for it in d.delivered_items.all():
                if it.has_discrepancy:
                    discrepancy_logs.append({
                        'delivery_id': d.id,
                        'order_id': d.order_id,
                        'partner_name': driver_name,
                        'product_name': it.product.name if it.product else 'Item',
                        'ordered_qty': it.ordered_quantity,
                        'delivered_qty': it.delivered_quantity,
                        'notes': it.discrepancy_notes,
                    })

        partners_list = list(partner_stats.values())
        partners_list.sort(key=lambda x: x['delivered'], reverse=True)

        return {
            'date_filter': range_info,
            'summary': {
                'total_deliveries': total_deliveries,
                'delivered': delivered_count,
                'failed': failed_count,
                'in_progress': in_progress_count,
                'completion_rate_pct': completion_rate,
                'discrepancies_count': len(discrepancy_logs),
            },
            'partners': partners_list,
            'discrepancies': discrepancy_logs[:100],
        }

    @classmethod
    def get_discounts_report(cls, range_info: dict) -> dict:
        """
        Discounts Report:
        - Source: Finalized Invoices with discount_amount > 0 in date range
        - Total discounts granted, impact on gross revenue, invoice breakdown
        """
        start_dt = range_info['start_dt']
        end_dt = range_info['end_dt']

        invoices_qs = Invoice.objects.filter(
            created_at__gte=start_dt,
            created_at__lte=end_dt,
            discount_amount__gt=Decimal('0.00'),
        ).exclude(status='void').select_related('customer')

        agg = invoices_qs.aggregate(
            total_discount=Sum('discount_amount'),
            discounted_invoices_count=Count('id'),
            total_subtotal=Sum('subtotal'),
            total_grand_total=Sum('grand_total'),
        )

        total_discount = agg['total_discount'] or Decimal('0.00')
        invoices_count = agg['discounted_invoices_count'] or 0
        total_subtotal = agg['total_subtotal'] or Decimal('0.00')

        avg_discount_pct = round((total_discount / total_subtotal * 100), 2) if total_subtotal > Decimal('0.00') else 0.0

        discount_rows = [
            {
                'id': inv.id,
                'invoice_number': inv.invoice_number,
                'customer_name': inv.customer_name,
                'subtotal': str(inv.subtotal),
                'discount_amount': str(inv.discount_amount),
                'grand_total': str(inv.grand_total),
                'discount_pct': round((inv.discount_amount / inv.subtotal * 100), 1) if inv.subtotal > 0 else 0.0,
                'created_at': inv.created_at.astimezone(KOLKATA_TZ).strftime('%Y-%m-%d %H:%M'),
            }
            for inv in invoices_qs.order_by('-discount_amount')[:200]
        ]

        return {
            'date_filter': range_info,
            'summary': {
                'total_discount_amount': str(total_discount),
                'discounted_invoices_count': invoices_count,
                'average_discount_pct': avg_discount_pct,
            },
            'invoices': discount_rows,
        }

    @classmethod
    def get_profit_loss_report(cls, range_info: dict) -> dict:
        """
        Profit & Loss Report:
        - Net Sales = sum of Invoice.grand_total (finalized non-void invoices)
        - COGS = sum of InvoiceItem.total_cost snapshots
        - Gross Profit = Net Sales - COGS
        - Operating Expenses = sum of Expense.amount (strictly excludes raw materials/COGS)
        - Net Profit = Gross Profit - Operating Expenses
        - Cost Coverage Metadata: 'complete', 'partial', or 'unavailable'
        """
        start_dt = range_info['start_dt']
        end_dt = range_info['end_dt']

        # 1. Finalized Invoices Sales
        invoices_qs = Invoice.objects.filter(
            created_at__gte=start_dt,
            created_at__lte=end_dt,
        ).exclude(status='void')

        net_sales = invoices_qs.aggregate(s=Sum('grand_total'))['s'] or Decimal('0.00')
        invoice_count = invoices_qs.count()

        # 2. COGS from InvoiceItems
        items_qs = InvoiceItem.objects.filter(
            invoice__in=invoices_qs
        )
        total_items_count = items_qs.count()
        items_with_cost = items_qs.filter(total_cost__isnull=False)
        items_with_cost_count = items_with_cost.count()

        cogs_captured = items_with_cost.aggregate(s=Sum('total_cost'))['s'] or Decimal('0.00')

        # Cost coverage classification
        if total_items_count == 0:
            coverage_status = 'complete'
        elif items_with_cost_count == total_items_count:
            coverage_status = 'complete'
        elif items_with_cost_count > 0:
            coverage_status = 'partial'
        else:
            coverage_status = 'unavailable'

        # 3. Operating Expenses in date range
        start_date = range_info['from_date']
        end_date = range_info['to_date']

        expenses_qs = Expense.objects.filter(
            expense_date__gte=start_date,
            expense_date__lte=end_date,
        )
        total_expenses = expenses_qs.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

        # Expense breakdown by category
        exp_by_cat = expenses_qs.values('category').annotate(
            total=Sum('amount'),
            count=Count('id')
        ).order_by('-total')

        category_breakdown = [
            {
                'category': e['category'],
                'category_display': dict(Expense.CATEGORY_CHOICES).get(e['category'], e['category']),
                'total': str(e['total'] or Decimal('0.00')),
                'count': e['count'],
            }
            for e in exp_by_cat
        ]

        # 4. Profit Computations
        gross_profit = net_sales - cogs_captured
        net_profit = gross_profit - total_expenses
        profit_margin_pct = round((net_profit / net_sales * 100), 2) if net_sales > Decimal('0.00') else 0.0

        return {
            'date_filter': range_info,
            'summary': {
                'net_sales': str(net_sales),
                'cogs': str(cogs_captured),
                'cost_coverage_status': coverage_status,
                'items_with_cost_count': items_with_cost_count,
                'total_items_count': total_items_count,
                'gross_profit': str(gross_profit),
                'operating_expenses': str(total_expenses),
                'net_profit': str(net_profit),
                'profit_margin_pct': profit_margin_pct,
            },
            'expense_categories': category_breakdown,
        }

    @classmethod
    def get_unified_report(cls, range_info: dict) -> dict:
        """
        Unified Business Report (Phase R5):
        Consolidates all business and financial dimensions into ONE unified dataset:
        A. Business Summary (KPIs, Financials, Portfolio Outstanding, Orders, Deliveries, P&L)
        B. Sales & Invoice Details (All finalized invoices in period)
        C. Collection & Payment Transactions (Payments in period, methods, references, unallocated, allocations)
        D. Customer-wise Outstanding (Current pending, overdue, advance credits, last payment date)
        E. Customer Payment History (Chronological payment allocation audit trail)
        F. Discounts & Expenses (Discounts summary & operating expenses breakdown with individual entries)
        G. Delivery Summary (Fulfillment statuses, partner performance, quantity discrepancies)
        H. Profit & Loss (Net Sales, COGS, Gross Profit, Operating Expenses, Net Profit, Coverage status)
        """
        start_dt = range_info['start_dt']
        end_dt = range_info['end_dt']
        start_date = range_info['from_date']
        end_date = range_info['to_date']
        today = timezone.now().astimezone(KOLKATA_TZ).date()

        # 1. Sales & Invoices in period
        invoices_qs = Invoice.objects.filter(
            created_at__gte=start_dt,
            created_at__lte=end_dt,
        ).exclude(status='void').select_related('customer', 'company', 'order').prefetch_related('invoice_orders', 'allocations', 'payments')

        sales_agg = invoices_qs.aggregate(
            gross_subtotal=Sum('subtotal'),
            total_discounts=Sum('discount_amount'),
            net_sales=Sum('grand_total'),
            invoice_count=Count('id'),
        )
        gross_subtotal = sales_agg['gross_subtotal'] or Decimal('0.00')
        total_discounts = sales_agg['total_discounts'] or Decimal('0.00')
        net_sales = sales_agg['net_sales'] or Decimal('0.00')
        invoice_count = sales_agg['invoice_count'] or 0

        # Detailed Invoices List
        sales_invoices = []
        for inv in invoices_qs.order_by('-created_at'):
            is_cons = getattr(inv, 'invoice_orders', None) and inv.invoice_orders.count() > 1
            source_lbl = 'Consolidated' if is_cons else ('Single Order' if inv.order_id else 'Quick Billing')
            cust_name = inv.customer.name if inv.customer else (inv.company.name if inv.company else inv.customer_name)
            cust_phone = inv.customer.phone if inv.customer else (inv.company.phone if inv.company else inv.customer_phone)
            paid_amount = inv.total_paid
            pending_amt = inv.pending_amount

            sales_invoices.append({
                'id': inv.id,
                'invoice_number': inv.invoice_number,
                'invoice_date': inv.created_at.astimezone(KOLKATA_TZ).strftime('%Y-%m-%d %H:%M'),
                'customer_name': cust_name,
                'customer_phone': cust_phone,
                'source': source_lbl,
                'gross_amount': str(inv.subtotal),
                'discount_amount': str(inv.discount_amount),
                'net_invoice_amount': str(inv.grand_total),
                'amount_paid': str(paid_amount),
                'pending_amount': str(pending_amt),
                'status': inv.status,
                'due_date': inv.due_date.isoformat() if inv.due_date else None,
            })

        # 2. Collections & Payments in period
        payments_qs = Payment.objects.filter(
            received_at__gte=start_dt,
            received_at__lte=end_dt,
        ).select_related('customer', 'company').prefetch_related('allocations__invoice')

        coll_agg = payments_qs.aggregate(
            total_collections=Sum('amount'),
            payment_count=Count('id')
        )
        total_collections = coll_agg['total_collections'] or Decimal('0.00')
        payment_count = coll_agg['payment_count'] or 0

        payment_transactions = []
        for p in payments_qs.order_by('-received_at'):
            cust_name = p.customer.name if p.customer else (p.company.name if p.company else 'Direct Account')
            alloc_invoices = []
            for alloc in p.allocations.all():
                alloc_invoices.append(f"{alloc.invoice.invoice_number} (₹{alloc.allocated_amount})")
            invoices_str = ', '.join(alloc_invoices) if alloc_invoices else 'Unallocated'

            payment_transactions.append({
                'id': p.id,
                'customer_name': cust_name,
                'payment_date': p.received_at.astimezone(KOLKATA_TZ).strftime('%Y-%m-%d %H:%M'),
                'amount_received': str(p.amount),
                'method': p.get_method_display(),
                'reference_number': p.reference_number or '-',
                'allocated_amount': str(p.total_allocated),
                'unallocated_advance': str(p.unallocated_amount),
                'is_advance': p.unallocated_amount > Decimal('0.00'),
                'allocated_invoices': invoices_str,
            })

        # 3. Customer-wise Outstanding & Portfolio Balances (As of Report Generation Date)
        all_unpaid_invoices = Invoice.objects.exclude(status__in=['paid', 'void']).select_related('customer', 'company').prefetch_related('allocations', 'payments')

        total_portfolio_outstanding = Decimal('0.00')
        total_overdue_outstanding = Decimal('0.00')
        overdue_invoices_count = 0

        customer_outstanding_map = {}

        for inv in all_unpaid_invoices:
            pending = inv.pending_amount
            if pending <= Decimal('0.00'):
                continue

            total_portfolio_outstanding += pending

            is_overdue = False
            if inv.due_date is not None and inv.due_date < today:
                is_overdue = True
                total_overdue_outstanding += pending
                overdue_invoices_count += 1

            cust_key = inv.customer_id or f"comp_{inv.company_id}" or "walkin"
            cust_name = inv.customer.name if inv.customer else (inv.company.name if inv.company else inv.customer_name)
            cust_phone = inv.customer.phone if inv.customer else (inv.company.phone if inv.company else inv.customer_phone)

            if cust_key not in customer_outstanding_map:
                customer_outstanding_map[cust_key] = {
                    'customer_id': inv.customer_id,
                    'customer_name': cust_name,
                    'phone': cust_phone,
                    'current_outstanding': Decimal('0.00'),
                    'overdue_balance': Decimal('0.00'),
                    'unpaid_invoices_count': 0,
                }
            customer_outstanding_map[cust_key]['current_outstanding'] += pending
            if is_overdue:
                customer_outstanding_map[cust_key]['overdue_balance'] += pending
            customer_outstanding_map[cust_key]['unpaid_invoices_count'] += 1

        # Customer advance credits across all payments
        all_payments_all_time = Payment.objects.all().prefetch_related('allocations')
        total_customer_advance_credits = Decimal('0.00')
        customer_advance_map = {}
        for p in all_payments_all_time:
            unalloc = p.unallocated_amount
            if unalloc > Decimal('0.00'):
                total_customer_advance_credits += unalloc
                cust_key = p.customer_id or f"comp_{p.company_id}" or "walkin"
                customer_advance_map[cust_key] = customer_advance_map.get(cust_key, Decimal('0.00')) + unalloc

        # Enrich customer-wise records with lifetime totals and last payment date
        customers_qs = Customer.objects.all().prefetch_related('invoices', 'payments')
        customer_outstanding_rows = []
        for c in customers_qs:
            cust_key = c.id
            c_invoices = c.invoices.exclude(status='void')
            total_billed = c_invoices.aggregate(s=Sum('grand_total'))['s'] or Decimal('0.00')
            total_collected = c.payments.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
            current_due = customer_outstanding_map.get(cust_key, {}).get('current_outstanding', Decimal('0.00'))
            overdue_due = customer_outstanding_map.get(cust_key, {}).get('overdue_balance', Decimal('0.00'))
            advance_cr = customer_advance_map.get(cust_key, Decimal('0.00'))

            last_payment = c.payments.order_by('-received_at').first()
            last_payment_date = last_payment.received_at.astimezone(KOLKATA_TZ).strftime('%Y-%m-%d') if last_payment else '-'

            if total_billed > 0 or total_collected > 0 or current_due > 0 or advance_cr > 0:
                customer_outstanding_rows.append({
                    'customer_id': c.id,
                    'customer_name': c.name,
                    'phone': c.phone or '-',
                    'total_billed': str(total_billed),
                    'total_collected': str(total_collected),
                    'current_outstanding': str(current_due),
                    'overdue_balance': str(overdue_due),
                    'advance_credit': str(advance_cr),
                    'last_payment_date': last_payment_date,
                })

        customer_outstanding_rows.sort(key=lambda x: Decimal(x['current_outstanding']), reverse=True)

        # 4. Customer Payment History (Chronological audit of allocations within period)
        allocations_in_period = PaymentAllocation.objects.filter(
            payment__received_at__gte=start_dt,
            payment__received_at__lte=end_dt,
        ).select_related('payment', 'payment__customer', 'invoice').order_by('-payment__received_at')

        payment_history = []
        for al in allocations_in_period:
            p = al.payment
            cname = p.customer.name if p.customer else 'Direct Account'
            payment_history.append({
                'customer_name': cname,
                'payment_date': p.received_at.astimezone(KOLKATA_TZ).strftime('%Y-%m-%d %H:%M'),
                'payment_amount': str(p.amount),
                'payment_method': p.get_method_display(),
                'reference': p.reference_number or '-',
                'invoice_allocation': f"{al.invoice.invoice_number} (₹{al.allocated_amount})",
                'remaining_balance': str(al.invoice.pending_amount),
            })

        # 5. Discounts & Operating Expenses
        # Discounts
        discounted_invoices_qs = invoices_qs.filter(discount_amount__gt=Decimal('0.00'))
        discounted_invoices = [
            {
                'invoice_number': inv.invoice_number,
                'customer_name': inv.customer_name,
                'invoice_date': inv.created_at.astimezone(KOLKATA_TZ).strftime('%Y-%m-%d'),
                'subtotal': str(inv.subtotal),
                'discount_amount': str(inv.discount_amount),
                'grand_total': str(inv.grand_total),
                'discount_pct': round((inv.discount_amount / inv.subtotal * 100), 1) if inv.subtotal > 0 else 0.0,
            }
            for inv in discounted_invoices_qs.order_by('-discount_amount')
        ]

        # Expenses
        expenses_qs = Expense.objects.filter(
            expense_date__gte=start_date,
            expense_date__lte=end_date,
        ).order_by('-expense_date', '-created_at')

        total_operating_expenses = expenses_qs.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

        expense_list = [
            {
                'id': exp.id,
                'title': exp.title,
                'category': exp.category,
                'category_display': exp.get_category_display(),
                'amount': str(exp.amount),
                'expense_date': exp.expense_date.isoformat(),
                'payment_method': exp.get_payment_method_display(),
            }
            for exp in expenses_qs
        ]

        # 6. Delivery Summary
        deliveries_qs = Delivery.objects.filter(
            order__created_at__gte=start_dt,
            order__created_at__lte=end_dt,
        ).select_related('order', 'driver', 'driver__partner_profile').prefetch_related('delivered_items')

        total_deliveries = deliveries_qs.count()
        delivered_count = deliveries_qs.filter(status='delivered').count()
        failed_count = deliveries_qs.filter(status='failed').count()
        pending_deliveries_count = deliveries_qs.filter(status='pending').count()
        in_progress_count = total_deliveries - (delivered_count + failed_count + pending_deliveries_count)
        completion_rate = round((delivered_count / total_deliveries * 100), 1) if total_deliveries > 0 else 0.0

        partner_stats = {}
        discrepancies = []
        for d in deliveries_qs:
            driver_name = d.driver.partner_profile.name if hasattr(d.driver, 'partner_profile') and d.driver.partner_profile else (d.driver.username if d.driver else 'Unassigned')
            d_id = d.driver_id or 0
            if d_id not in partner_stats:
                partner_stats[d_id] = {'partner_name': driver_name, 'assigned': 0, 'delivered': 0, 'failed': 0}
            partner_stats[d_id]['assigned'] += 1
            if d.status == 'delivered':
                partner_stats[d_id]['delivered'] += 1
            elif d.status == 'failed':
                partner_stats[d_id]['failed'] += 1

            for it in d.delivered_items.all():
                if it.has_discrepancy:
                    discrepancies.append({
                        'delivery_id': d.id,
                        'order_id': d.order_id,
                        'partner_name': driver_name,
                        'product_name': it.product.name if it.product else 'Item',
                        'ordered_qty': it.ordered_quantity,
                        'delivered_qty': it.delivered_quantity,
                        'notes': it.discrepancy_notes,
                    })

        partners_roster = list(partner_stats.values())
        partners_roster.sort(key=lambda x: x['delivered'], reverse=True)

        # 7. Profit & Loss Computations
        items_qs = InvoiceItem.objects.filter(invoice__in=invoices_qs)
        total_items_count = items_qs.count()
        items_with_cost = items_qs.filter(total_cost__isnull=False)
        items_with_cost_count = items_with_cost.count()
        cogs_captured = items_with_cost.aggregate(s=Sum('total_cost'))['s'] or Decimal('0.00')

        if total_items_count == 0:
            coverage_status = 'complete'
        elif items_with_cost_count == total_items_count:
            coverage_status = 'complete'
        elif items_with_cost_count > 0:
            coverage_status = 'partial'
        else:
            coverage_status = 'unavailable'

        gross_profit = net_sales - cogs_captured
        net_profit = gross_profit - total_operating_expenses
        profit_margin_pct = round((net_profit / net_sales * 100), 2) if net_sales > Decimal('0.00') else 0.0

        # Orders count in period
        from orders.models import Order
        total_orders_count = Order.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt).count()

        return {
            'date_filter': range_info,
            'summary': {
                'gross_sales': str(gross_subtotal),
                'total_discounts': str(total_discounts),
                'net_sales': str(net_sales),
                'total_collections': str(total_collections),
                'portfolio_outstanding': str(total_portfolio_outstanding),
                'overdue_outstanding': str(total_overdue_outstanding),
                'total_advance_credits': str(total_customer_advance_credits),
                'total_invoices': invoice_count,
                'total_payment_transactions': payment_count,
                'total_orders': total_orders_count,
                'completed_deliveries': delivered_count,
                'operating_expenses': str(total_operating_expenses),
                'cogs': str(cogs_captured),
                'gross_profit': str(gross_profit),
                'net_profit': str(net_profit),
                'profit_margin_pct': profit_margin_pct,
                'cost_coverage_status': coverage_status,
                'items_with_cost_count': items_with_cost_count,
                'total_items_count': total_items_count,
            },
            'sales_invoices': sales_invoices,
            'collection_transactions': payment_transactions,
            'customer_outstanding': customer_outstanding_rows,
            'customer_payment_history': payment_history,
            'discounts': {
                'total_discount_amount': str(total_discounts),
                'discounted_invoices_count': len(discounted_invoices),
                'invoices': discounted_invoices,
            },
            'expenses': {
                'total_expenses': str(total_operating_expenses),
                'items': expense_list,
            },
            'deliveries': {
                'total_deliveries': total_deliveries,
                'delivered': delivered_count,
                'failed': failed_count,
                'pending': pending_deliveries_count,
                'in_progress': in_progress_count,
                'completion_rate_pct': completion_rate,
                'partners': partners_roster,
                'discrepancies': discrepancies,
            },
            'profit_loss': {
                'net_sales': str(net_sales),
                'cogs': str(cogs_captured),
                'cost_coverage_status': coverage_status,
                'gross_profit': str(gross_profit),
                'operating_expenses': str(total_operating_expenses),
                'net_profit': str(net_profit),
                'profit_margin_pct': profit_margin_pct,
            },
        }
