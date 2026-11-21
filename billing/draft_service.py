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
        delivery_partner=None,
        delivery_address: str = '',
        notes: str = '',
        created_by=None,
    ) -> DraftInvoice:
        """
        Create a new draft invoice atomically:
        - items_data: list of dicts [{'product_id': int, 'product_name': str, 'unit': str, 'quantity': int, 'unit_price': Decimal}]
        - order_ids: list of order PKs to reserve (for single or consolidated order billing)
        - delivery_address: destination address for delivery drafts
        """
        from decimal import Decimal

        if billing_source not in dict(DraftInvoice.BILLING_SOURCE_CHOICES):
            raise ValidationError({'billing_source': f"Invalid billing source '{billing_source}'."})

        # Phase 5C.1 Validation: Deliverable Quick Billing drafts require a valid destination address
        clean_delivery_address = (delivery_address or '').strip()
        if billing_source == 'quick_billing' and delivery_partner:
            if not customer:
                if not clean_delivery_address:
                    raise ValidationError({'delivery_address': "Delivery address is required when assigning a delivery partner to a walk-in customer."})
            else:
                effective_address = clean_delivery_address or (customer.address.strip() if customer.address else '')
                if not effective_address:
                    raise ValidationError({'delivery_address': "Delivery address is required for delivery orders. Customer has no saved address, please provide an address."})
                if not clean_delivery_address and customer.address:
                    clean_delivery_address = customer.address.strip()

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
                delivery_partner=delivery_partner,
                delivery_address=clean_delivery_address,
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

                        # Phase R1: Capture product cost snapshot if available
                        prod_cost = getattr(it.product, 'cost_price', None) if it.product else None
                        unit_cost_val = prod_cost if prod_cost is not None and prod_cost > Decimal('0.00') else (Decimal('0.00') if prod_cost is not None else None)
                        total_cost_val = (unit_cost_val * it.quantity) if unit_cost_val is not None else None

                        DraftInvoiceItem.objects.create(
                            draft=draft,
                            product=it.product,
                            product_name=p_name,
                            unit=unit_str,
                            quantity=it.quantity,
                            unit_price=u_price,
                            total_price=line_total,
                            unit_cost=unit_cost_val,
                            total_cost=total_cost_val,
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

                    # Phase R1: Allow explicit unit_cost or capture from Product.cost_price
                    raw_cost = item.get('unit_cost')
                    if raw_cost is not None:
                        try:
                            unit_cost_val = Decimal(str(raw_cost))
                        except (ValueError, TypeError):
                            unit_cost_val = None
                    elif prod and getattr(prod, 'cost_price', None) is not None:
                        unit_cost_val = prod.cost_price
                    else:
                        unit_cost_val = None

                    total_cost_val = (unit_cost_val * qty) if unit_cost_val is not None else None

                    DraftInvoiceItem.objects.create(
                        draft=draft,
                        product=prod,
                        product_name=p_name,
                        unit=unit_str,
                        quantity=qty,
                        unit_price=u_price,
                        total_price=line_total,
                        unit_cost=unit_cost_val,
                        total_cost=total_cost_val,
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
        delivery_partner=None,
        clear_delivery_partner: bool = False,
        delivery_address: str = None,
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

            if clear_delivery_partner:
                locked_draft.delivery_partner = None
            elif delivery_partner is not None:
                locked_draft.delivery_partner = delivery_partner

            if delivery_address is not None:
                locked_draft.delivery_address = delivery_address.strip()

            if notes is not None:
                locked_draft.notes = notes

            # Phase 5C.1 Validation on Update:
            if locked_draft.billing_source == 'quick_billing' and locked_draft.delivery_partner:
                eff_address = (locked_draft.delivery_address or '').strip()
                if not locked_draft.customer:
                    if not eff_address:
                        raise ValidationError({'delivery_address': "Delivery address is required when assigning a delivery partner to a walk-in customer."})
                else:
                    if not eff_address and locked_draft.customer.address:
                        eff_address = locked_draft.customer.address.strip()
                        locked_draft.delivery_address = eff_address
                    if not eff_address:
                        raise ValidationError({'delivery_address': "Delivery address is required for delivery orders. Customer has no saved address, please provide an address."})

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

                    # Phase R1: Capture / recalculate unit_cost & total_cost
                    raw_cost = item.get('unit_cost')
                    if raw_cost is not None:
                        try:
                            unit_cost_val = Decimal(str(raw_cost))
                        except (ValueError, TypeError):
                            unit_cost_val = None
                    elif prod and getattr(prod, 'cost_price', None) is not None:
                        unit_cost_val = prod.cost_price
                    else:
                        unit_cost_val = None

                    total_cost_val = (unit_cost_val * qty) if unit_cost_val is not None else None

                    DraftInvoiceItem.objects.create(
                        draft=locked_draft,
                        product=prod,
                        product_name=p_name,
                        unit=unit_str,
                        quantity=qty,
                        unit_price=u_price,
                        total_price=line_total,
                        unit_cost=unit_cost_val,
                        total_cost=total_cost_val,
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
    def _create_fulfillment_order_and_delivery(cls, draft: DraftInvoice, official_invoice: Invoice, owner_user):
        """
        Phase 5B: Creates an operational Fulfillment Order container and Delivery record
        upon successful finalization of a Quick Billing draft with an assigned active Delivery Partner.
        Ensures strict idempotency and consistency across relationships.
        """
        from orders.models import Order, OrderItem
        from deliveries.models import Delivery, DeliveryStatusHistory
        from notifications.services import NotificationService

        # 1. Idempotency Check: Check if an Order is already linked to this invoice
        existing_order = Order.objects.filter(order_billing__invoice=official_invoice).first()
        if not existing_order and official_invoice.order:
            existing_order = official_invoice.order

        if existing_order:
            # Already created, ensure Delivery exists and avoid duplicate creation
            return existing_order

        # 2. Determine Order customer and customer_profile
        # Customer resolution: Order.customer must be a valid User (cannot be null)
        customer_user = None
        if draft.customer and getattr(draft.customer, 'user', None):
            customer_user = draft.customer.user
        elif owner_user and owner_user.is_authenticated:
            customer_user = owner_user
        elif draft.created_by and draft.created_by.is_authenticated:
            customer_user = draft.created_by
        else:
            # Fallback to first superuser or owner
            from django.contrib.auth.models import User
            customer_user = User.objects.filter(is_superuser=True).first() or User.objects.first()

        customer_profile = draft.customer
        company = draft.customer.company if (draft.customer and getattr(draft.customer, 'company', None)) else None

        delivery_address = ''
        if draft.delivery_address and draft.delivery_address.strip():
            delivery_address = draft.delivery_address.strip()
        elif draft.customer and draft.customer.address and draft.customer.address.strip():
            delivery_address = draft.customer.address.strip()
        else:
            delivery_address = "Walk-in / Counter Pickup"

        operational_notes = f"Fulfillment Order for Official Invoice #{official_invoice.invoice_number}."
        if draft.notes:
            operational_notes += f" {draft.notes}"

        # 3. Create operational Order container
        order = Order.objects.create(
            customer=customer_user,
            customer_profile=customer_profile,
            company=company,
            delivery_address=delivery_address,
            status='accepted',
            total_amount=draft.grand_total,
            notes=operational_notes,
        )

        # 4. Create OrderItems from DraftInvoiceItem snapshots
        for d_item in draft.items.all():
            OrderItem.objects.create(
                order=order,
                product=d_item.product,
                quantity=d_item.quantity,
                price=d_item.unit_price,
            )

        # 5. Link Order to Official Invoice
        InvoiceOrder.objects.create(
            invoice=official_invoice,
            order=order,
        )
        if not official_invoice.order:
            official_invoice.order = order
            official_invoice.save(update_fields=['order'])

        # 6. Create Delivery Record
        driver_user = draft.delivery_partner.user
        delivery, created = Delivery.objects.get_or_create(
            order=order,
            defaults={
                'driver': driver_user,
                'status': 'assigned',
            }
        )
        if not created and delivery.driver != driver_user:
            delivery.driver = driver_user
            delivery.save(update_fields=['driver'])

        # 7. Record initial DeliveryStatusHistory audit log
        DeliveryStatusHistory.objects.create(
            delivery=delivery,
            from_status='',
            to_status='assigned',
            changed_by=owner_user,
            notes=f"Assigned upon finalization of Quick Billing Invoice #{official_invoice.invoice_number}"
        )

        # 8. Dispatch notification (NotificationService.on_delivery_assigned itself wraps FCM/SMS dispatch inside transaction.on_commit)
        if driver_user:
            NotificationService.on_delivery_assigned(delivery)

        return order

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
                # Phase R1: Lock historical unit_cost and total_cost snapshots
                unit_cost_val = d_item.unit_cost
                if unit_cost_val is None and d_item.product and getattr(d_item.product, 'cost_price', None) is not None:
                    unit_cost_val = d_item.product.cost_price

                total_cost_val = (unit_cost_val * d_item.quantity) if unit_cost_val is not None else None

                InvoiceItem.objects.create(
                    invoice=official_invoice,
                    product=d_item.product,
                    product_name=d_item.product_name,
                    unit_price=d_item.unit_price,
                    quantity=d_item.quantity,
                    total_price=d_item.total_price,
                    unit_cost=unit_cost_val,
                    total_cost=total_cost_val,
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

            # Phase 5B: Operational Fulfillment Order and Delivery Dispatch for Quick Billing
            # Only Quick Billing drafts with an active Delivery Partner trigger fulfillment dispatch
            if locked_draft.billing_source == 'quick_billing' and locked_draft.delivery_partner:
                cls._create_fulfillment_order_and_delivery(locked_draft, official_invoice, owner_user)

            return official_invoice
