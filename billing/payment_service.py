from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .models import Company, Invoice, Payment, PaymentAllocation

class PaymentAllocationService:
    """
    Handles payment processing, FIFO invoice allocation, and advance credits:
    - Automatically allocates payments against company's oldest unpaid invoices (FIFO)
    - Supports partial payments
    - Captures leftover funds as company advance credit
    - Produces an audit trail of allocations in PaymentAllocation
    """

    @classmethod
    def allocate_payment_fifo(cls, payment: Payment, company: Company = None, target_invoices: list = None) -> dict:
        """
        Allocates payment amount across unpaid or partially paid invoices using FIFO.
        - If target_invoices is provided, allocates only to those.
        - Otherwise, finds all unpaid invoices for company in created_at ASC order.
        """
        with transaction.atomic():
            remaining_to_allocate = payment.amount - payment.total_allocated
            if remaining_to_allocate <= Decimal('0.00'):
                return {
                    'allocated_now': '0.00',
                    'total_allocated': str(payment.total_allocated),
                    'unallocated_advance': str(payment.unallocated_amount),
                    'invoices_settled': 0,
                }

            if not target_invoices:
                if company:
                    invoices_qs = Invoice.objects.filter(company=company).exclude(status__in=['paid', 'void']).order_by('created_at')
                elif payment.invoice:
                    invoices_qs = [payment.invoice]
                else:
                    invoices_qs = []
            else:
                invoices_qs = target_invoices

            allocated_now = Decimal('0.00')
            invoices_settled = 0

            for inv in invoices_qs:
                due = inv.pending_amount
                if due <= Decimal('0.00'):
                    continue

                pay_amt = min(remaining_to_allocate, due)
                PaymentAllocation.objects.create(
                    payment=payment,
                    invoice=inv,
                    allocated_amount=pay_amt,
                    notes=f"FIFO allocation from Payment #{payment.id}",
                )

                inv.update_payment_status()
                allocated_now += pay_amt
                remaining_to_allocate -= pay_amt
                if inv.status == 'paid':
                    invoices_settled += 1

                if remaining_to_allocate <= Decimal('0.00'):
                    break

            return {
                'allocated_now': str(allocated_now),
                'total_allocated': str(payment.total_allocated),
                'unallocated_advance': str(payment.unallocated_amount),
                'invoices_settled': invoices_settled,
            }
