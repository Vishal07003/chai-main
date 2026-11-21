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
    def resolve_item_unit_price(cls, customer_or_company, product, quantity: int) -> Decimal:
        """
        Resolves customer-specific custom price or company contracted price.
        Falls back to product.price if no custom rate applies.
        """
        from .models import Customer, CustomerProductPrice
        today = timezone.now().date()

        # 1. Check CustomerProductPrice first (Customer-first consolidated workflow)
        if isinstance(customer_or_company, Customer):
            cust_price = CustomerProductPrice.objects.filter(
                customer=customer_or_company,
                product=product,
                is_active=True
            ).first()
            if cust_price:
                return cust_price.custom_price
            # Fall back to customer's linked company if present
            if customer_or_company.company:
                customer_or_company = customer_or_company.company

        # 2. Check CompanyProductPrice (backward compatibility)
        if isinstance(customer_or_company, Company) and customer_or_company:
            slab = CompanyProductPrice.objects.filter(
                company=customer_or_company,
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
            customer = locked_order.customer_profile
            customer_name = customer.name if customer else (company.name if company else (locked_order.customer.get_full_name() or locked_order.customer.username))
            customer_phone = customer.phone if customer else (company.phone if company else (getattr(locked_order.customer.profile, 'phone', '') if hasattr(locked_order.customer, 'profile') else ''))

            date_str = timezone.now().strftime('%Y%m%d')
            # Generate unique invoice number by finding max suffix
            today_invoices = Invoice.objects.filter(invoice_number__startswith=f"INV-{date_str}-")
            max_num = 0
            for ti in today_invoices:
                try:
                    num_part = int(ti.invoice_number.split('-')[-1])
                    if num_part > max_num:
                        max_num = num_part
                except (ValueError, IndexError):
                    pass
            invoice_number = f"INV-{date_str}-{(max_num + 1):04d}"

            invoice = Invoice.objects.create(
                invoice_number=invoice_number,
                order=locked_order,
                company=company,
                customer=customer,
                customer_name=customer_name,
                customer_phone=customer_phone,
                status='unpaid',
                notes=notes,
                generated_by=owner_user,
            )

            subtotal = Decimal('0.00')
            total_discount = Decimal('0.00')

            pricing_entity = customer or company

            for item_data in confirmed_items:
                product_id = item_data.get('product_id')
                qty = int(item_data.get('quantity', 1))
                if qty <= 0:
                    continue

                from products.models import Product
                product = Product.objects.get(id=product_id)

                # 1. Base rate resolution (backend only)
                base_unit_price = cls.resolve_item_unit_price(pricing_entity, product, qty)

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

                # Phase R1: Snapshot unit_cost & total_cost
                unit_cost_val = getattr(product, 'cost_price', None) if product else None
                total_cost_val = (unit_cost_val * qty) if unit_cost_val is not None else None

                InvoiceItem.objects.create(
                    invoice=invoice,
                    product=product,
                    product_name=product.name,
                    unit_price=effective_unit_price,
                    quantity=qty,
                    total_price=line_total,
                    unit_cost=unit_cost_val,
                    total_cost=total_cost_val,
                )

            grand_total = max(Decimal('0.00'), subtotal - total_discount)
            invoice.subtotal = subtotal
            invoice.discount_amount = total_discount
            invoice.grand_total = grand_total
            invoice.save(update_fields=['subtotal', 'discount_amount', 'grand_total'])

            return invoice
