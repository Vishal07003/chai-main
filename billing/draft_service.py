from django.db import transaction
from rest_framework.exceptions import ValidationError
from billing.models import (
    Customer,
    Invoice,
    InvoiceItem,
    InvoiceOrder,
    DraftInvoice,
    DraftInvoiceItem,
    DraftInvoiceOrder,
)
from orders.models import Order


class DraftBillingService:
    """
    Phase 1 Draft Billing Domain Service:
    - Creates and manages editable DraftInvoice and DraftInvoiceItem instances.
    - Manages multi-order reservations via DraftInvoiceOrder.
    - Enforces transactional constraints:
        1. All selected orders must belong to the exact same customer.
        2. Selected orders must not already have an official invoice (across both legacy Invoice.order and InvoiceOrder).
        3. An order cannot be reserved by more than one active draft.
        4. Cancelling or deleting a draft releases all reserved orders.
        5. Attaching orders to an official invoice verifies cross-relationship uniqueness.
    """

    @classmethod
    def create_draft(
        cls,
        customer: Customer = None,
        billing_source: str = 'quick_billing',
        items_data: list = None,
        order_ids: list = None,
        discount_amount=0,
        due_date=None,
        notes: str = '',
        created_by=None,
    ) -> DraftInvoice:
        """
        Create a new draft invoice atomically:
        - items_data: list of dicts [{'product_id': int, 'product_name': str, 'unit': str, 'quantity': int, 'unit_price': Decimal}]
        - order_ids: list of order PKs to reserve (for single or consolidated order billing)
        """
        from decimal import Decimal

        if billing_source not in dict(DraftInvoice.BILLING_SOURCE_CHOICES):
            raise ValidationError({'billing_source': f"Invalid billing source '{billing_source}'."})

        with transaction.atomic():
            # 1. Validate customer & orders
            orders = []
            if order_ids:
                orders = list(Order.objects.select_for_update().filter(id__in=order_ids))
                if len(orders) != len(order_ids):
                    found_ids = {o.id for o in orders}
                    missing = set(order_ids) - found_ids
                    raise ValidationError({'order_ids': f"Orders not found: {list(missing)}"})

                # Rule A: All selected orders must belong to the same customer
                first_cust = orders[0].customer_profile
                for ord_obj in orders:
                    if ord_obj.customer_profile != first_cust:
                        raise ValidationError({
                            'order_ids': "All selected orders must belong to the exact same customer."
                        })

                if customer and first_cust and customer.id != first_cust.id:
                    raise ValidationError({
                        'customer': "Selected customer does not match the orders' customer."
                    })

                if not customer:
                    customer = first_cust

                # Rule B: Selected orders must NOT already be officially invoiced
                for ord_obj in orders:
                    if ord_obj.has_invoice:
                        raise ValidationError({
                            'order_ids': f"Order #{ord_obj.id} already has an official invoice and cannot be billed again."
                        })

                # Rule C: One order cannot be reserved by multiple active drafts
                active_reservations = DraftInvoiceOrder.objects.filter(
                    order__in=orders,
                    draft__status='draft',
                ).select_related('draft', 'order')

                if active_reservations.exists():
                    res_info = [f"Order #{r.order_id} in {r.draft.draft_number}" for r in active_reservations]
                    raise ValidationError({
                        'order_ids': f"Orders are already reserved by another active draft: {', '.join(res_info)}"
                    })

            # 2. Build DraftInvoice instance
            draft_num = DraftInvoice.generate_draft_number()
            cust_name = customer.name if customer else 'Walk-in Customer'
            cust_phone = customer.phone if customer else ''

            draft = DraftInvoice.objects.create(
                draft_number=draft_num,
                customer=customer,
                customer_name=cust_name,
                customer_phone=cust_phone,
                billing_source=billing_source,
                status='draft',
                subtotal=Decimal('0.00'),
                discount_amount=Decimal(str(discount_amount or 0)),
                grand_total=Decimal('0.00'),
                due_date=due_date,
                notes=notes,
                created_by=created_by,
            )

            # 3. Reserve orders via DraftInvoiceOrder
            for ord_obj in orders:
                DraftInvoiceOrder.objects.create(draft=draft, order=ord_obj)

            # 4. Populate draft items
            subtotal = Decimal('0.00')
            items_data = items_data or []

            # If items_data not explicitly provided but orders exist, populate items from orders
            if not items_data and orders:
                for ord_obj in orders:
                    for it in ord_obj.items.select_related('product'):
                        p_name = it.product.name if it.product else 'Item'
                        unit_str = getattr(it.product, 'unit', 'Pc') or 'Pc'
                        u_price = it.price
                        line_total = u_price * it.quantity
                        subtotal += line_total
                        DraftInvoiceItem.objects.create(
                            draft=draft,
                            product=it.product,
                            product_name=p_name,
                            unit=unit_str,
                            quantity=it.quantity,
                            unit_price=u_price,
                            total_price=line_total,
                        )
            else:
                from products.models import Product
                for item in items_data:
                    pid = item.get('product_id')
                    prod = Product.objects.filter(id=pid).first() if pid else None
                    p_name = item.get('product_name') or (prod.name if prod else 'Item')
                    unit_str = item.get('unit') or (getattr(prod, 'unit', 'Pc') if prod else 'Pc')
                    qty = int(item.get('quantity', 1))
                    u_price = Decimal(str(item.get('unit_price', prod.price if prod else 0)))
                    line_total = u_price * qty
                    subtotal += line_total

                    DraftInvoiceItem.objects.create(
                        draft=draft,
                        product=prod,
                        product_name=p_name,
                        unit=unit_str,
                        quantity=qty,
                        unit_price=u_price,
                        total_price=line_total,
                    )

            disc = Decimal(str(discount_amount or 0))
            if disc > subtotal:
                raise ValidationError({'discount_amount': 'Discount amount cannot exceed subtotal.'})

            draft.subtotal = subtotal
            draft.grand_total = max(Decimal('0.00'), subtotal - disc)
            draft.save(update_fields=['subtotal', 'grand_total'])

            return draft

    @classmethod
    def update_draft(
        cls,
        draft: DraftInvoice,
        customer: Customer = None,
        items_data: list = None,
        order_ids: list = None,
        discount_amount=None,
        due_date=None,
        notes: str = None,
    ) -> DraftInvoice:
        """
        Updates an existing draft atomically:
        - Prevents changing finalized or cancelled drafts.
        - Recalculates subtotal and grand_total on backend.
        - Updates order reservations if order_ids are provided.
        - Updates line items if items_data is provided.
        """
        from decimal import Decimal

        with transaction.atomic():
            locked_draft = DraftInvoice.objects.select_for_update().get(id=draft.id)
            if locked_draft.status != 'draft':
                raise ValidationError({
                    'detail': f"Cannot modify draft in '{locked_draft.get_status_display()}' status."
                })

            # 1. Update customer if allowed
            if customer is not None:
                locked_draft.customer = customer
                locked_draft.customer_name = customer.name
                locked_draft.customer_phone = customer.phone

            if due_date is not None:
                locked_draft.due_date = due_date

            if notes is not None:
                locked_draft.notes = notes

            # 2. Update order reservations if order_ids provided
            if order_ids is not None:
                orders = list(Order.objects.select_for_update().filter(id__in=order_ids))
                if len(orders) != len(order_ids):
                    found_ids = {o.id for o in orders}
                    missing = set(order_ids) - found_ids
                    raise ValidationError({'order_ids': f"Orders not found: {list(missing)}"})

                target_customer = locked_draft.customer
                if orders:
                    first_cust = orders[0].customer_profile
                    for ord_obj in orders:
                        if ord_obj.customer_profile != first_cust:
                            raise ValidationError({
                                'order_ids': "All selected orders must belong to the exact same customer."
                            })
                    if target_customer and first_cust and target_customer.id != first_cust.id:
                        raise ValidationError({
                            'customer': "Selected customer does not match the orders' customer."
                        })
                    if not target_customer and first_cust:
                        locked_draft.customer = first_cust
                        locked_draft.customer_name = first_cust.name
                        locked_draft.customer_phone = first_cust.phone

                    for ord_obj in orders:
                        if ord_obj.has_invoice:
                            raise ValidationError({
                                'order_ids': f"Order #{ord_obj.id} already has an official invoice."
                            })

                    # Check other active drafts
                    other_reservations = DraftInvoiceOrder.objects.filter(
                        order__in=orders,
                        draft__status='draft',
                    ).exclude(draft=locked_draft).select_related('draft', 'order')

                    if other_reservations.exists():
                        res_info = [f"Order #{r.order_id} in {r.draft.draft_number}" for r in other_reservations]
                        raise ValidationError({
                            'order_ids': f"Orders are already reserved by another active draft: {', '.join(res_info)}"
                        })

                # Refresh draft orders
                locked_draft.draft_orders.all().delete()
                for ord_obj in orders:
                    DraftInvoiceOrder.objects.create(draft=locked_draft, order=ord_obj)

            # 3. Update items if provided
            if items_data is not None:
                locked_draft.items.all().delete()
                from products.models import Product
                subtotal = Decimal('0.00')
                for item in items_data:
                    pid = item.get('product_id')
                    prod = Product.objects.filter(id=pid).first() if pid else None
                    p_name = item.get('product_name') or (prod.name if prod else 'Item')
                    unit_str = item.get('unit') or (getattr(prod, 'unit', 'Pc') if prod else 'Pc')
                    qty = int(item.get('quantity', 1))
                    u_price = Decimal(str(item.get('unit_price', prod.price if prod else 0)))
                    line_total = u_price * qty
                    subtotal += line_total

                    DraftInvoiceItem.objects.create(
                        draft=locked_draft,
                        product=prod,
                        product_name=p_name,
                        unit=unit_str,
                        quantity=qty,
                        unit_price=u_price,
                        total_price=line_total,
                    )
                locked_draft.subtotal = subtotal
            else:
                subtotal = locked_draft.subtotal

            if discount_amount is not None:
                disc = Decimal(str(discount_amount))
                if disc > subtotal:
                    raise ValidationError({'discount_amount': 'Discount amount cannot exceed subtotal.'})
                locked_draft.discount_amount = disc
            else:
                disc = locked_draft.discount_amount

            locked_draft.grand_total = max(Decimal('0.00'), subtotal - disc)
            locked_draft.save()

            return locked_draft

    @classmethod
    def cancel_draft(cls, draft: DraftInvoice) -> DraftInvoice:
        """
        Cancels a draft, immediately releasing all order reservations.
        """
        with transaction.atomic():
            draft.status = 'cancelled'
            draft.save(update_fields=['status'])
            # Note: DraftInvoiceOrder records remain for audit history, but because draft.status != 'draft',
            # the orders are immediately freed for future drafts or invoicing.
            return draft

    @classmethod
    def attach_orders_to_official_invoice(cls, invoice: Invoice, orders: list) -> list:
        """
        Attaches verified orders to an official invoice via InvoiceOrder:
        - Rejects if any order is already linked to another official invoice across legacy or InvoiceOrder.
        """
        with transaction.atomic():
            created_links = []
            for ord_obj in orders:
                # Cross-relationship duplicate check
                if ord_obj.has_invoice:
                    raise ValidationError({
                        'order': f"Order #{ord_obj.id} is already attached to an official invoice."
                    })

                io = InvoiceOrder.objects.create(
                    invoice=invoice,
                    order=ord_obj,
                )
                created_links.append(io)
            return created_links

    @classmethod
    def finalize_draft(cls, draft_id: int, owner_user, notes: str = None) -> Invoice:
        """
        Finalizes a DraftInvoice into an official Invoice atomically:
        1. Select for update lock on DraftInvoice.
        2. Verify status == 'draft'.
        3. Revalidate all attached orders (no duplicate official invoices).
        4. Validate items and calculated totals.
        5. Generate official invoice number (INV-YYYYMMDD-XXXX).
        6. Create official Invoice record with due_date, finalized_at, and generated_by.
        7. Create immutable InvoiceItems.
        8. Create InvoiceOrder consolidation records (zero for quick billing).
        9. Link official_invoice to DraftInvoice and set status='finalized'.
        10. Preserves all draft data for audit history.
        """
        from decimal import Decimal
        from django.utils import timezone

        with transaction.atomic():
            locked_draft = DraftInvoice.objects.select_for_update().get(id=draft_id)
            if locked_draft.status != 'draft':
                raise ValidationError({
                    'detail': f"Draft #{locked_draft.draft_number} cannot be finalized because its status is '{locked_draft.get_status_display()}'."
                })

            draft_items = list(locked_draft.items.all())
            if not draft_items:
                raise ValidationError({'detail': "Draft invoice has no product items to finalize."})

            # Revalidate all reserved orders
            draft_orders = list(locked_draft.draft_orders.select_related('order').all())
            orders_to_link = []
            for d_ord in draft_orders:
                ord_obj = Order.objects.select_for_update().get(id=d_ord.order_id)
                if ord_obj.has_invoice:
                    raise ValidationError({
                        'order': f"Order #{ord_obj.id} has already been officially invoiced."
                    })
                orders_to_link.append(ord_obj)

            # Generate unique official invoice number
            date_str = timezone.now().strftime('%Y%m%d')
            today_invoices = Invoice.objects.filter(invoice_number__startswith=f"INV-{date_str}-")
            max_num = 0
            for ti in today_invoices:
                try:
                    num_part = int(ti.invoice_number.split('-')[-1])
                    if num_part > max_num:
                        max_num = num_part
                except (ValueError, IndexError):
                    pass
            official_invoice_number = f"INV-{date_str}-{(max_num + 1):04d}"

            final_notes = notes if notes is not None else locked_draft.notes
            customer = locked_draft.customer
            company = customer.company if customer else None

            # Legacy 1:1 order field: populate only if exactly 1 order, otherwise None (multi-orders consolidated in InvoiceOrder)
            legacy_order = orders_to_link[0] if len(orders_to_link) == 1 else None

            official_invoice = Invoice.objects.create(
                invoice_number=official_invoice_number,
                order=legacy_order,
                company=company,
                customer=customer,
                customer_name=locked_draft.customer_name,
                customer_phone=locked_draft.customer_phone,
                subtotal=locked_draft.subtotal,
                discount_amount=locked_draft.discount_amount,
                grand_total=locked_draft.grand_total,
                status='unpaid',
                due_date=locked_draft.due_date,
                finalized_at=timezone.now(),
                notes=final_notes,
                generated_by=owner_user,
            )

            # Create official InvoiceItems
            for d_item in draft_items:
                InvoiceItem.objects.create(
                    invoice=official_invoice,
                    product=d_item.product,
                    product_name=d_item.product_name,
                    unit_price=d_item.unit_price,
                    quantity=d_item.quantity,
                    total_price=d_item.total_price,
                )

            # Create InvoiceOrder linking records
            for ord_obj in orders_to_link:
                InvoiceOrder.objects.create(
                    invoice=official_invoice,
                    order=ord_obj,
                )

            # Update Draft reference
            locked_draft.status = 'finalized'
            locked_draft.official_invoice = official_invoice
            locked_draft.save(update_fields=['status', 'official_invoice', 'updated_at'])

            # Phase 3: Automatic advance allocation upon finalization
            from billing.payment_service import PaymentAllocationService
            PaymentAllocationService.allocate_customer_advances_on_invoice(official_invoice)

            return official_invoice
