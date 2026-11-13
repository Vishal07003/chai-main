from decimal import Decimal
from django.db import transaction, models
from django.utils import timezone
from rest_framework.exceptions import ValidationError, PermissionDenied
from .models import Company, Invoice, InvoiceItem, CompanyProductPrice
from orders.models import Order, PriceOverrideRequest

class InvoicingService:
    """
    Handles manual invoice generation from Orders:
    - Never trusts client-submitted prices or totals.
    - Recalculates unit prices via CompanyProductPrice slabs or Product catalog price.
    - Applies owner-approved discounts only.
    - Enforces 1:1 Order-to-Invoice uniqueness with database transactions.
    """

    @classmethod
    def resolve_item_unit_price(cls, company: Company, product, quantity: int) -> Decimal:
        """
        Resolves contracted company price based on quantity slabs and active date.
        Falls back to product.price if no company slab applies.
        """
        today = timezone.now().date()
        if company:
            slab = CompanyProductPrice.objects.filter(
                company=company,
                product=product,
                is_active=True,
                min_quantity__lte=quantity,
                valid_from__lte=today,
            ).filter(
                models.Q(valid_to__isnull=True) | models.Q(valid_to__gte=today)
            ).order_by('-min_quantity').first()

            if slab:
                return slab.custom_price

        return product.price

    @classmethod
    def generate_manual_invoice(cls, order: Order, confirmed_items: list, owner_user, notes: str = '') -> Invoice:
        """
        Generates official Invoice from an Order under an atomic transaction:
        - confirmed_items: list of dicts: [{'product_id': int, 'quantity': int}]
        - Validates owner permission.
        - Validates that order does not already have an invoice.
        - Recalculates all pricing on the backend.
        """
        if not owner_user.is_staff and not owner_user.is_superuser:
            if not (hasattr(owner_user, 'profile') and owner_user.profile.role == 'owner'):
                raise PermissionDenied("Only owners can manually generate invoices.")

        with transaction.atomic():
            # Lock order row for update
            locked_order = Order.objects.select_for_update().get(id=order.id)
            if hasattr(locked_order, 'invoice') and locked_order.invoice:
                raise ValidationError({"error": f"Invoice #{locked_order.invoice.invoice_number} already exists for Order #{locked_order.id}."})

            company = locked_order.company
            customer_name = company.name if company else (locked_order.customer.get_full_name() or locked_order.customer.username)
            customer_phone = company.phone if company else (getattr(locked_order.customer.profile, 'phone', '') if hasattr(locked_order.customer, 'profile') else '')

            date_str = timezone.now().strftime('%Y%m%d')
            # Generate unique invoice number
            last_inv = Invoice.objects.filter(invoice_number__startswith=f"INV-{date_str}").count()
            invoice_number = f"INV-{date_str}-{(last_inv + 1):04d}"

            invoice = Invoice.objects.create(
                invoice_number=invoice_number,
                order=locked_order,
                company=company,
                customer_name=customer_name,
                customer_phone=customer_phone,
                status='unpaid',
                notes=notes,
                generated_by=owner_user,
            )

            subtotal = Decimal('0.00')
            total_discount = Decimal('0.00')

            for item_data in confirmed_items:
                product_id = item_data.get('product_id')
                qty = int(item_data.get('quantity', 1))
                if qty <= 0:
                    continue

                from products.models import Product
                product = Product.objects.get(id=product_id)

                # 1. Base rate resolution (backend only)
                base_unit_price = cls.resolve_item_unit_price(company, product, qty)

                # 2. Check for Owner-Approved Price Override for this item
                approved_override = PriceOverrideRequest.objects.filter(
                    order=locked_order,
                    product=product,
                    status='APPROVED',
                ).order_by('-decision_timestamp').first()

                effective_unit_price = approved_override.requested_price if approved_override else base_unit_price
                line_total = effective_unit_price * qty
                subtotal += (base_unit_price * qty)

                if approved_override and approved_override.requested_price < base_unit_price:
                    total_discount += ((base_unit_price - approved_override.requested_price) * qty)

                InvoiceItem.objects.create(
                    invoice=invoice,
                    product=product,
                    product_name=product.name,
                    unit_price=effective_unit_price,
                    quantity=qty,
                    total_price=line_total,
                )

            grand_total = max(Decimal('0.00'), subtotal - total_discount)
            invoice.subtotal = subtotal
            invoice.discount_amount = total_discount
            invoice.grand_total = grand_total
            invoice.save(update_fields=['subtotal', 'discount_amount', 'grand_total'])

            return invoice
