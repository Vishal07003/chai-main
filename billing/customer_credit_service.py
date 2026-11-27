from decimal import Decimal
from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from .models import Customer, Company, Invoice, CustomerCreditTransaction


class CustomerCreditService:
    """
    Phase 5 & 6 Centralized Customer Credit & Refund Due Engine:
    - Pure, immutable financial transaction ledger
    - Excess payment allocation after invoice correction (Credit Balance vs. Refund Due)
    - Balance calculation:
        Available Credit = CREDIT_ADDED - CREDIT_APPLIED
        Pending Refund Due = REFUND_DUE - REFUND_SETTLED
    - Safe application of credit balance against pending invoices
    - Manual settlement record for offline cash/UPI refund
    - Never modifies bank accounts or integrates automatic payment gateways
    """

    @classmethod
    def get_balances(cls, customer: Customer = None, company: Company = None) -> dict:
        """
        Computes accurate available credit balance and pending refund due.
        """
        if not customer and not company:
            return {'available_credit': Decimal('0.00'), 'refund_due': Decimal('0.00')}

        q = models.Q()
        if customer:
            q |= models.Q(customer=customer)
        if company:
            q |= models.Q(company=company)

        txs = CustomerCreditTransaction.objects.filter(q)

        credit_added = txs.filter(transaction_type='CREDIT_ADDED').aggregate(s=models.Sum('amount'))['s'] or Decimal('0.00')
        credit_applied = txs.filter(transaction_type='CREDIT_APPLIED').aggregate(s=models.Sum('amount'))['s'] or Decimal('0.00')
        refund_due = txs.filter(transaction_type='REFUND_DUE').aggregate(s=models.Sum('amount'))['s'] or Decimal('0.00')
        refund_settled = txs.filter(transaction_type='REFUND_SETTLED').aggregate(s=models.Sum('amount'))['s'] or Decimal('0.00')

        available_credit = max(Decimal('0.00'), credit_added - credit_applied)
        pending_refund = max(Decimal('0.00'), refund_due - refund_settled)

        return {
            'available_credit': available_credit,
            'refund_due': pending_refund,
        }

    @classmethod
    def record_excess_treatment(
        cls,
        invoice: Invoice,
        treatment: str,  # 'credit' or 'refund'
        amount: Decimal,
        recorded_by: User,
        reason: str = ''
    ) -> CustomerCreditTransaction:
        """
        Records an immutable credit or refund due transaction for excess payment after correction.
        """
        if amount <= Decimal('0.00'):
            raise ValidationError("Excess amount must be positive.")

        treatment_clean = treatment.lower().strip()
        if treatment_clean not in ('credit', 'refund'):
            raise ValidationError("Treatment must be either 'credit' or 'refund'.")

        tx_type = 'CREDIT_ADDED' if treatment_clean == 'credit' else 'REFUND_DUE'
        default_reason = (
            f"Excess payment of ₹{amount} from Invoice #{invoice.invoice_number or invoice.id} correction designated as "
            f"{'Customer Credit Balance' if treatment_clean == 'credit' else 'Refund Due'}."
        )

        with transaction.atomic():
            tx = CustomerCreditTransaction.objects.create(
                customer=invoice.customer,
                company=invoice.company,
                invoice=invoice,
                transaction_type=tx_type,
                amount=amount,
                reason=reason.strip() if reason else default_reason,
                recorded_by=recorded_by,
            )
            return tx

    @classmethod
    def apply_credit_to_invoice(
        cls,
        invoice: Invoice,
        amount: Decimal,
        applied_by: User,
        reason: str = ''
    ) -> CustomerCreditTransaction:
        """
        Applies available customer credit to pay/settle an invoice.
        """
        if amount <= Decimal('0.00'):
            raise ValidationError("Credit application amount must be positive.")

        with transaction.atomic():
            balances = cls.get_balances(customer=invoice.customer, company=invoice.company)
            avail = balances['available_credit']

            if amount > avail:
                raise ValidationError(f"Cannot apply ₹{amount}; only ₹{avail} credit balance available.")

            due = invoice.pending_amount
            if amount > due:
                raise ValidationError(f"Cannot apply ₹{amount}; invoice outstanding is only ₹{due}.")

            tx = CustomerCreditTransaction.objects.create(
                customer=invoice.customer,
                company=invoice.company,
                invoice=invoice,
                transaction_type='CREDIT_APPLIED',
                amount=amount,
                reason=reason.strip() if reason else f"Credit applied to Invoice #{invoice.invoice_number or invoice.id}",
                recorded_by=applied_by,
            )

            # Record a zero-cash/credit Payment linked to invoice so invoice & allocation status update cleanly
            from .payment_service import PaymentAllocationService
            pm = PaymentAllocationService.record_payment(
                amount=amount,
                method='cash',  # internal allocation tag
                customer=invoice.customer,
                company=invoice.company,
                invoice=invoice,
                notes=f"Settled via Customer Credit Balance (Tx #{tx.id})",
                recorded_by=applied_by,
            )
            PaymentAllocationService.allocate_payment_fifo(
                payment=pm,
                target_invoices=[invoice]
            )

            invoice.refresh_from_db()
            return tx

    @classmethod
    def settle_refund(
        cls,
        customer: Customer = None,
        company: Company = None,
        amount: Decimal = Decimal('0.00'),
        settled_by: User = None,
        reason: str = ''
    ) -> CustomerCreditTransaction:
        """
        Records manual offline payout of pending refund due to customer.
        """
        if amount <= Decimal('0.00'):
            raise ValidationError("Settlement amount must be positive.")

        with transaction.atomic():
            balances = cls.get_balances(customer=customer, company=company)
            pending_refund = balances['refund_due']

            if amount > pending_refund:
                raise ValidationError(f"Cannot settle ₹{amount}; pending refund due is only ₹{pending_refund}.")

            tx = CustomerCreditTransaction.objects.create(
                customer=customer,
                company=company,
                transaction_type='REFUND_SETTLED',
                amount=amount,
                reason=reason.strip() if reason else "Manual offline refund handed/transferred to customer",
                recorded_by=settled_by,
            )
            return tx
