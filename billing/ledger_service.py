import calendar
from datetime import date, datetime, time
from decimal import Decimal
from django.db import models
from django.utils import timezone
from .models import Company, Invoice, Payment, PaymentAllocation, LedgerAdjustment

class LedgerService:
    """
    Dynamic Company Financial Ledger & Monthly Statement Engine:
    - Pure read-only computation (never creates or alters invoice/payment records)
    - Dynamic calculation of Opening Balance (< start_date)
    - Chronological event timeline (Invoices, Payments, Adjustments)
    - Running balance calculation
    - Dynamic Closing Balance
    """

    @classmethod
    def get_monthly_statement(cls, company: Company, year: int, month: int) -> dict:
        """
        Calculate statement for given company, year, and month.
        Formula:
          Opening Balance = Prior Debits - Prior Credits
          Period Running Balance = Opening + Debits - Credits
          Closing Balance = Period Running Balance at period end
        """
        _, last_day = calendar.monthrange(year, month)
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)

        start_dt = timezone.make_aware(datetime.combine(start_date, time.min))
        end_dt = timezone.make_aware(datetime.combine(end_date, time.max))

        # --- 1. Opening Balance Calculation (< start_dt) ---
        prior_invoiced = Invoice.objects.filter(
            company=company,
            created_at__lt=start_dt
        ).exclude(status='void').aggregate(s=models.Sum('grand_total'))['s'] or Decimal('0.00')

        prior_debit_adjustments = LedgerAdjustment.objects.filter(
            company=company,
            adjustment_type='DEBIT',
            created_at__lt=start_dt
        ).aggregate(s=models.Sum('amount'))['s'] or Decimal('0.00')

        # Prior payments linked directly or via company
        prior_payments = Payment.objects.filter(
            models.Q(company=company) | models.Q(invoice__company=company),
            received_at__lt=start_dt
        ).distinct().aggregate(s=models.Sum('amount'))['s'] or Decimal('0.00')

        prior_credit_adjustments = LedgerAdjustment.objects.filter(
            company=company,
            adjustment_type='CREDIT',
            created_at__lt=start_dt
        ).aggregate(s=models.Sum('amount'))['s'] or Decimal('0.00')

        opening_balance = (prior_invoiced + prior_debit_adjustments) - (prior_payments + prior_credit_adjustments)

        # --- 2. Period Transactions (start_dt <= created_at <= end_dt) ---
        events = []

        # A. Invoices (Debits)
        period_invoices = Invoice.objects.filter(
            company=company,
            created_at__gte=start_dt,
            created_at__lte=end_dt
        ).exclude(status='void').order_by('created_at')

        for inv in period_invoices:
            events.append({
                'timestamp': inv.created_at,
                'type': 'INVOICE',
                'reference_no': inv.invoice_number,
                'description': f"Daily Bill: {inv.items.count()} items",
                'debit': inv.grand_total,
                'credit': Decimal('0.00'),
                'entity_id': inv.id,
            })

        # B. Payments (Credits)
        period_payments = Payment.objects.filter(
            models.Q(company=company) | models.Q(invoice__company=company),
            received_at__gte=start_dt,
            received_at__lte=end_dt
        ).distinct().order_by('received_at')

        for pm in period_payments:
            desc = f"Payment via {pm.get_method_display()}"
            if pm.reference_number:
                desc += f" (Ref: {pm.reference_number})"
            events.append({
                'timestamp': pm.received_at,
                'type': 'PAYMENT',
                'reference_no': f"PAY-{pm.id:06d}",
                'description': desc,
                'debit': Decimal('0.00'),
                'credit': pm.amount,
                'entity_id': pm.id,
            })

        # C. Adjustments (Debits/Credits)
        period_adjustments = LedgerAdjustment.objects.filter(
            company=company,
            created_at__gte=start_dt,
            created_at__lte=end_dt
        ).order_by('created_at')

        for adj in period_adjustments:
            is_credit = adj.adjustment_type == 'CREDIT'
            events.append({
                'timestamp': adj.created_at,
                'type': f"ADJUSTMENT_{adj.adjustment_type}",
                'reference_no': f"ADJ-{adj.id:06d}",
                'description': adj.reason,
                'debit': Decimal('0.00') if is_credit else adj.amount,
                'credit': adj.amount if is_credit else Decimal('0.00'),
                'entity_id': adj.id,
            })

        # --- 3. Sort Chronologically & Compute Running Balance ---
        events.sort(key=lambda x: x['timestamp'])

        running_balance = opening_balance
        timeline = []

        total_period_debit = Decimal('0.00')
        total_period_credit = Decimal('0.00')

        for ev in events:
            debit = ev['debit']
            credit = ev['credit']
            running_balance += (debit - credit)
            total_period_debit += debit
            total_period_credit += credit

            timeline.append({
                'date': ev['timestamp'].isoformat(),
                'type': ev['type'],
                'reference_no': ev['reference_no'],
                'description': ev['description'],
                'debit': str(debit),
                'credit': str(credit),
                'running_balance': str(running_balance),
                'entity_id': ev['entity_id'],
            })

        closing_balance = running_balance

        return {
            'company': {
                'id': company.id,
                'name': company.name,
                'trade_name': company.trade_name,
                'phone': company.phone,
                'gstin': company.gstin,
                'billing_cycle': company.billing_cycle,
                'credit_limit': str(company.credit_limit),
            },
            'period': {
                'year': year,
                'month': month,
                'start_date': start_date.isoformat(),
                'end_date': end_date.isoformat(),
            },
            'summary': {
                'opening_balance': str(opening_balance),
                'total_debit': str(total_period_debit),
                'total_credit': str(total_period_credit),
                'closing_balance': str(closing_balance),
            },
            'transactions': timeline,
        }

