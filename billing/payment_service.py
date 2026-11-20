from decimal import Decimal
from django.db import transaction, models
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .models import Company, Customer, Invoice, Payment, PaymentAllocation


class PaymentAllocationService:
    """
    Centralized Payment and Allocation Domain Service:
    - Recording customer payments and invoice-specific payments
    - Calculating available unallocated credit
    - Allocating payments using FIFO policy
    - Maintaining partial payment allocations
    - Maintaining customer-level advance credits
    - Returning accurate invoice outstanding balances
    - Preventing duplicate or excessive allocations
    - Concurrency protection via row-level locking (select_for_update)
    """

    @classmethod
    def record_payment(
        cls,
        amount,
        method: str = 'cash',
        customer: Customer = None,
        company: Company = None,
        invoice: Invoice = None,
        reference_number: str = '',
        notes: str = '',
        recorded_by=None,
    ) -> Payment:
        """
        Record a financial payment atomically with validation.
        Does not perform auto-allocation by itself; callers invoke allocate_payment_fifo.
        """
        try:
            amt = Decimal(str(amount))
        except (ValueError, TypeError):
            raise ValidationError({'amount': 'Valid payment amount is required.'})

        if amt <= Decimal('0.00'):
            raise ValidationError({'amount': 'Payment amount must be greater than zero.'})

        normalized_method = (method or 'cash').lower()
        valid_methods = dict(Payment.METHOD_CHOICES).keys()
        if normalized_method not in valid_methods:
            normalized_method = 'cash'

        # Auto-resolve customer / company if invoice is given
        if invoice:
            if not customer and invoice.customer:
                customer = invoice.customer
            if not company and invoice.company:
                company = invoice.company

        if customer and not company and customer.company:
            company = customer.company

        payment = Payment.objects.create(
            customer=customer,
            company=company,
            invoice=invoice,
            amount=amt,
            method=normalized_method,
            reference_number=reference_number or '',
            notes=notes or '',
            recorded_by=recorded_by,
        )
        return payment

    @classmethod
    def allocate_payment_fifo(
        cls,
        payment: Payment,
        customer: Customer = None,
        company: Company = None,
        target_invoices: list = None,
    ) -> dict:
        """
        Allocates a payment across eligible unpaid or partially paid official invoices using FIFO.
        Uses select_for_update() to prevent race conditions during concurrent allocation.
        """
        with transaction.atomic():
            # Lock the payment row to calculate remaining strictly
            locked_payment = Payment.objects.select_for_update().get(id=payment.id)
            remaining_to_allocate = locked_payment.amount - locked_payment.total_allocated

            if remaining_to_allocate <= Decimal('0.00'):
                return {
                    'allocated_now': '0.00',
                    'total_allocated': str(locked_payment.total_allocated),
                    'unallocated_advance': str(locked_payment.unallocated_amount),
                    'invoices_settled': 0,
                    'settled_invoices': [],
                }

            if target_invoices is not None:
                # Target invoices explicitly passed - lock them
                target_ids = [inv.id for inv in target_invoices]
                invoices_qs = Invoice.objects.select_for_update().filter(id__in=target_ids).exclude(status__in=['paid', 'void']).order_by('created_at', 'id')
            elif customer:
                invoices_qs = Invoice.objects.select_for_update().filter(customer=customer).exclude(status__in=['paid', 'void']).order_by('created_at', 'id')
            elif company:
                invoices_qs = Invoice.objects.select_for_update().filter(company=company).exclude(status__in=['paid', 'void']).order_by('created_at', 'id')
            elif locked_payment.invoice:
                invoices_qs = Invoice.objects.select_for_update().filter(id=locked_payment.invoice_id).exclude(status__in=['paid', 'void'])
            elif locked_payment.customer:
                invoices_qs = Invoice.objects.select_for_update().filter(customer=locked_payment.customer).exclude(status__in=['paid', 'void']).order_by('created_at', 'id')
            elif locked_payment.company:
                invoices_qs = Invoice.objects.select_for_update().filter(company=locked_payment.company).exclude(status__in=['paid', 'void']).order_by('created_at', 'id')
            else:
                invoices_qs = Invoice.objects.none()

            allocated_now = Decimal('0.00')
            settled_invoices = []

            for inv in invoices_qs:
                # Calculate true due balance on this invoice for this payment
                alloc_sum = inv.allocations.aggregate(s=models.Sum('allocated_amount'))['s'] or Decimal('0.00')
                legacy_unalloc = inv.payments.filter(allocations__isnull=True).exclude(id=locked_payment.id).aggregate(s=models.Sum('amount'))['s'] or Decimal('0.00')
                due = max(Decimal('0.00'), inv.grand_total - (alloc_sum + legacy_unalloc))
                if due <= Decimal('0.00'):
                    continue

                pay_amt = min(remaining_to_allocate, due)
                if pay_amt <= Decimal('0.00'):
                    continue

                PaymentAllocation.objects.create(
                    payment=locked_payment,
                    invoice=inv,
                    allocated_amount=pay_amt,
                    notes=f"FIFO allocation from Payment #{locked_payment.id}",
                )

                inv.update_payment_status()
                allocated_now += pay_amt
                remaining_to_allocate -= pay_amt

                if inv.status == 'paid':
                    settled_invoices.append(inv.id)

                if remaining_to_allocate <= Decimal('0.00'):
                    break

            # Refresh payment property values
            return {
                'allocated_now': str(allocated_now),
                'total_allocated': str(locked_payment.total_allocated),
                'unallocated_advance': str(locked_payment.unallocated_amount),
                'invoices_settled': len(settled_invoices),
                'settled_invoices': settled_invoices,
            }

    @classmethod
    def get_unallocated_advance(cls, customer: Customer = None, company: Company = None) -> Decimal:
        """
        Calculates total available unallocated advance credit for a customer or company.
        """
        if not customer and not company:
            return Decimal('0.00')

        q = models.Q()
        if customer:
            q |= models.Q(customer=customer)
        if company:
            q |= models.Q(company=company)

        payments = Payment.objects.filter(q)
        total_advance = sum(p.unallocated_amount for p in payments)
        return total_advance

    @classmethod
    def allocate_customer_advances_on_invoice(cls, invoice: Invoice) -> dict:
        """
        Allocates any existing unallocated advance credits belonging to the invoice's customer
        against this invoice (and any older unpaid invoices via FIFO).
        Must be called within an atomic transaction (e.g. inside DraftBillingService.finalize_draft).
        """
        if not invoice.customer and not invoice.company:
            return {
                'allocated_from_advance': '0.00',
                'remaining_advance': '0.00',
                'invoice_status': invoice.status,
                'invoice_pending': str(invoice.pending_amount),
            }

        with transaction.atomic():
            # Lock customer/company payments that might have unallocated credit, oldest first (FIFO)
            q = models.Q()
            if invoice.customer:
                q |= models.Q(customer=invoice.customer)
            if invoice.company:
                q |= models.Q(company=invoice.company)

            candidate_payments = (
                Payment.objects.select_for_update()
                .filter(q)
                .order_by('received_at', 'id')
            )

            total_allocated_to_invoice = Decimal('0.00')

            for pm in candidate_payments:
                unallocated = pm.unallocated_amount
                if unallocated <= Decimal('0.00'):
                    continue

                # Lock and evaluate all unpaid invoices for this customer/company in FIFO order
                # This ensures advance credit pays oldest unpaid invoices first, including this newly finalized one
                inv_q = models.Q()
                if invoice.customer:
                    inv_q |= models.Q(customer=invoice.customer)
                if invoice.company:
                    inv_q |= models.Q(company=invoice.company)

                unpaid_invoices = (
                    Invoice.objects.select_for_update()
                    .filter(inv_q)
                    .exclude(status__in=['paid', 'void'])
                    .order_by('created_at', 'id')
                )

                remaining_pm_credit = unallocated
                for target_inv in unpaid_invoices:
                    due = target_inv.pending_amount
                    if due <= Decimal('0.00'):
                        continue

                    alloc_amount = min(remaining_pm_credit, due)
                    if alloc_amount <= Decimal('0.00'):
                        continue

                    PaymentAllocation.objects.create(
                        payment=pm,
                        invoice=target_inv,
                        allocated_amount=alloc_amount,
                        notes=f"Auto advance allocation from Payment #{pm.id} upon invoice finalization",
                    )

                    target_inv.update_payment_status()
                    remaining_pm_credit -= alloc_amount

                    if target_inv.id == invoice.id:
                        total_allocated_to_invoice += alloc_amount

                    if remaining_pm_credit <= Decimal('0.00'):
                        break

            # Re-read invoice status after allocations
            invoice.refresh_from_db()
            rem_advance = cls.get_unallocated_advance(customer=invoice.customer, company=invoice.company)

            return {
                'allocated_from_advance': str(total_allocated_to_invoice),
                'remaining_advance': str(rem_advance),
                'invoice_status': invoice.status,
                'invoice_pending': str(invoice.pending_amount),
            }
