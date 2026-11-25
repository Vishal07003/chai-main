import logging
from datetime import date, time, datetime, timedelta
from decimal import Decimal
import zoneinfo

from django.db import transaction
from django.utils import timezone
from django.contrib.auth.models import User

from orders.models import (
    Order,
    OrderItem,
    DailyDeliverySchedule,
    DailyDeliveryScheduleItem,
    DailyScheduleOrderExecution,
    ScheduleCancellationException,
    ScheduleNotificationTracker,
)
from deliveries.models import Delivery, DeliveryPartner
from billing.invoicing_service import InvoicingService
from notifications.services import NotificationService
from notifications.models import NotificationLog

logger = logging.getLogger(__name__)

KOLKATA_TZ = zoneinfo.ZoneInfo('Asia/Kolkata')


class DailyScheduleService:
    """
    Core Automation Service for Daily Delivery Schedules:
    1. Scheduled Order Generation (with database-level idempotency protection)
    2. Date-specific Cancellation (CASE A: before generation, CASE B: after generation)
    3. Automated FCM Notification Dispatch & Escalation Reminders
    """

    @classmethod
    def get_current_kolkata_time(cls) -> datetime:
        return timezone.now().astimezone(KOLKATA_TZ)

    @classmethod
    def generate_orders_for_date(cls, target_date: date = None) -> dict:
        """
        Executes order generation for all active schedules for target_date.
        - Guarantees exactly ONE order per (schedule, target_date).
        - Assigns the fixed Delivery Partner.
        - Enforces idempotency via DailyScheduleOrderExecution.
        - Skips cancelled dates (ScheduleCancellationException).
        - Sets scheduled_delivery_date and scheduled_delivery_time on the Order.
        - Initializes ScheduleNotificationTracker for FCM schedule management.
        """
        if target_date is None:
            target_date = cls.get_current_kolkata_time().date()

        schedules = DailyDeliverySchedule.objects.filter(is_active=True).select_related(
            'customer_profile', 'customer_user', 'delivery_partner', 'delivery_partner__user'
        ).prefetch_related('items', 'items__product', 'cancellation_exceptions')

        generated_orders = []
        skipped_existing = []
        skipped_ineligible = []

        for schedule in schedules:
            # 1. Eligibility check (start_date, end_date, weekday, cancellation exceptions)
            if not schedule.is_eligible_for_date(target_date):
                skipped_ineligible.append(schedule.id)
                continue

            # 2. Strict Idempotency check: exactly 1 order per schedule per delivery date
            if DailyScheduleOrderExecution.objects.filter(schedule=schedule, delivery_date=target_date).exists():
                skipped_existing.append(schedule.id)
                continue

            order = cls._create_scheduled_order(schedule, target_date)
            if order:
                generated_orders.append(order)

        logger.info(
            f"[DailyScheduleService] Date: {target_date} -> Generated: {len(generated_orders)}, "
            f"Already Existed: {len(skipped_existing)}, Ineligible: {len(skipped_ineligible)}"
        )

        return {
            'target_date': str(target_date),
            'generated_count': len(generated_orders),
            'generated_order_ids': [o.id for o in generated_orders],
            'skipped_existing_count': len(skipped_existing),
            'skipped_ineligible_count': len(skipped_ineligible),
        }

    @classmethod
    def _create_scheduled_order(cls, schedule: DailyDeliverySchedule, target_date: date) -> Order:
        """
        Atomically creates the Order, line items, fixed Delivery, execution ledger,
        and notification tracker.
        """
        items = schedule.items.select_related('product').all()
        if not items.exists():
            logger.warning(f"Schedule #{schedule.id} has no products configured. Skipping.")
            return None

        pricing_entity = schedule.customer_profile or getattr(schedule.customer_profile, 'company', None)
        delivery_address = schedule.delivery_address or getattr(schedule.customer_profile, 'address', '') or 'Store Counter'

        with transaction.atomic():
            # Check execution race condition inside transaction
            if DailyScheduleOrderExecution.objects.filter(schedule=schedule, delivery_date=target_date).exists():
                return None

            total = Decimal('0.00')
            order = Order.objects.create(
                customer=schedule.customer_user,
                customer_profile=schedule.customer_profile,
                company=getattr(schedule.customer_profile, 'company', None),
                status='pending',
                total_amount=Decimal('0.00'),
                delivery_address=delivery_address,
                scheduled_delivery_date=target_date,
                scheduled_delivery_time=schedule.delivery_time,
                notes=schedule.notes or f"Daily Delivery Schedule #{schedule.id}",
            )

            for it in items:
                if it.unit_price and it.unit_price > 0:
                    unit_p = it.unit_price
                else:
                    unit_p = InvoicingService.resolve_item_unit_price(
                        pricing_entity, it.product, it.quantity
                    )
                OrderItem.objects.create(
                    order=order,
                    product=it.product,
                    quantity=it.quantity,
                    price=unit_p
                )
                total += (unit_p * it.quantity)

            order.total_amount = total
            order.save(update_fields=['total_amount'])

            # Fixed Delivery Partner Assignment (Never automatic reassignment)
            driver_user = schedule.delivery_partner.user if schedule.delivery_partner else None
            delivery = Delivery.objects.create(
                order=order,
                driver=driver_user,
                status='assigned'
            )

            # Record Idempotency Ledger
            DailyScheduleOrderExecution.objects.create(
                schedule=schedule,
                order=order,
                delivery_date=target_date
            )

            # Initialize Schedule Notification Tracker
            ScheduleNotificationTracker.objects.create(
                order=order,
                schedule=schedule,
                delivery_date=target_date,
                delivery_time=schedule.delivery_time,
            )

            # Base system creation log
            logger.info(f"Generated scheduled Order #{order.id} for Schedule #{schedule.id} on {target_date}")
            return order

    @classmethod
    def cancel_date_delivery(cls, schedule: DailyDeliverySchedule, target_date: date, reason: str = "") -> dict:
        """
        Date-specific cancellation for a customer's delivery schedule:
        CASE A: Order not generated yet -> Save ScheduleCancellationException.
        CASE B: Order already generated -> Mark specific Order as cancelled, cancel delivery,
                flag notification tracker, notify partner.
        Future scheduled dates remain active.
        """
        # Check if an order was already generated for this schedule on target_date
        execution = DailyScheduleOrderExecution.objects.filter(
            schedule=schedule,
            delivery_date=target_date
        ).select_related('order', 'order__delivery').first()

        if execution and execution.order:
            order = execution.order
            # Check if order is already delivered / approved
            if order.status == 'delivered':
                raise ValueError("Cannot cancel an already completed and approved delivery.")

            with transaction.atomic():
                order.status = 'cancelled'
                order.notes = (order.notes or '') + f" | Cancelled by Owner on {target_date}: {reason or 'Requested skip'}"
                order.save(update_fields=['status', 'notes'])

                if hasattr(order, 'delivery') and order.delivery:
                    order.delivery.status = 'failed'
                    order.delivery.failure_reason = f"Cancelled by Owner for {target_date}: {reason or 'Date skip'}"
                    order.delivery.save(update_fields=['status', 'failure_reason'])

                # Update notification tracker to stop any future reminders
                tracker = getattr(order, 'schedule_notification_tracker', None)
                if tracker:
                    tracker.is_cancelled = True
                    tracker.save(update_fields=['is_cancelled'])

                # Notify fixed assigned partner about cancellation
                if schedule.delivery_partner and schedule.delivery_partner.user:
                    cls._notify_partner_of_cancellation(schedule.delivery_partner.user, order, target_date, reason)

            return {
                'case': 'B',
                'order_id': order.id,
                'status': 'cancelled',
                'message': f"Order #{order.id} for {target_date} has been cancelled and partner notified.",
            }
        else:
            # CASE A: Order not generated yet. Save exception so scheduler skips this date.
            exc, created = ScheduleCancellationException.objects.get_or_create(
                schedule=schedule,
                cancelled_date=target_date,
                defaults={'reason': reason or "Cancelled by Store Owner"}
            )
            return {
                'case': 'A',
                'status': 'exception_logged',
                'cancelled_date': str(target_date),
                'message': f"Delivery for {target_date} has been skipped. No order will be generated.",
            }

    @classmethod
    def _notify_partner_of_cancellation(cls, partner_user: User, order: Order, delivery_date: date, reason: str = ""):
        """Sends immediate FCM push to the fixed assigned partner regarding cancelled order."""
        customer_name = order.customer_profile.name if order.customer_profile else (order.customer.get_full_name() or order.customer.username)
        title = f"Delivery Cancelled: Order #{order.id}"
        body = f"Scheduled delivery for {customer_name} on {delivery_date} has been cancelled by the Owner."
        payload = {
            'type': 'order_cancelled',
            'order_id': str(order.id),
            'delivery_date': str(delivery_date),
        }
        try:
            NotificationService.send_to_user(
                user=partner_user,
                title=title,
                body=body,
                data=payload
            )
        except Exception as e:
            logger.error(f"Failed to send cancellation notification to partner {partner_user.username}: {e}")

    @classmethod
    def process_notification_reminders(cls) -> dict:
        """
        Automated Notification Engine for Scheduled Deliveries:
        Evaluates active ScheduleNotificationTrackers for today in Asia/Kolkata.

        Schedule:
        1. (Target Time - Lead Time): Initial FCM push to assigned fixed partner.
        2. (Target Time - Lead Time + 10 min): 1 Reminder if partner has not accepted.
        3. (Target Time - Lead Time + 20 min): Owner alert if partner still has not accepted.

        Guarantees:
        - Only notifies the fixed assigned partner.
        - Stops reminders once partner accepts or if order is cancelled.
        - Idempotent and never sends duplicates.
        - Logs failures and alerts Owner if partner lacks valid FCM device token.
        """
        now_dt = cls.get_current_kolkata_time()
        today = now_dt.date()
        current_time = now_dt.time()

        trackers = ScheduleNotificationTracker.objects.filter(
            delivery_date=today,
            is_cancelled=False,
            partner_accepted_at__isnull=True
        ).select_related(
            'order', 'order__delivery', 'schedule', 'schedule__delivery_partner',
            'schedule__delivery_partner__user', 'schedule__customer_profile'
        )

        initial_sent = 0
        reminders_sent = 0
        owner_escalations_sent = 0

        for tracker in trackers:
            order = tracker.order
            delivery = getattr(order, 'delivery', None)

            # Check if partner has already accepted via normal order workflow
            if delivery and delivery.status in ['accepted', 'picked_up', 'on_the_way', 'delivered']:
                tracker.partner_accepted_at = timezone.now()
                tracker.save(update_fields=['partner_accepted_at'])
                continue

            # If order was cancelled
            if order.status == 'cancelled':
                tracker.is_cancelled = True
                tracker.save(update_fields=['is_cancelled'])
                continue

            partner_user = tracker.schedule.delivery_partner.user if tracker.schedule.delivery_partner else None
            lead_time = tracker.schedule.notification_lead_time_minutes or 30

            # Target delivery datetime
            target_dt = datetime.combine(today, tracker.delivery_time, tzinfo=KOLKATA_TZ)
            initial_notify_dt = target_dt - timedelta(minutes=lead_time)
            reminder_notify_dt = initial_notify_dt + timedelta(minutes=10)
            owner_escalate_dt = initial_notify_dt + timedelta(minutes=20)

            customer_name = tracker.schedule.customer_profile.name if tracker.schedule.customer_profile else "Customer"
            time_str = tracker.delivery_time.strftime("%I:%M %p")
            product_summary = " • ".join([f"{it.quantity}x {it.product.name}" for it in tracker.schedule.items.all()[:3]])

            # Step 1: Initial Partner Notification (Lead Time before delivery)
            if now_dt >= initial_notify_dt and not tracker.initial_notified_at:
                if partner_user:
                    success = cls._send_partner_delivery_alert(
                        partner_user=partner_user,
                        order=order,
                        customer_name=customer_name,
                        time_str=time_str,
                        product_summary=product_summary,
                        is_reminder=False
                    )
                    tracker.initial_notified_at = timezone.now()
                    if not success:
                        tracker.fcm_failure_logged = True
                        cls._alert_owner_of_partner_unreachable(tracker.schedule, order, "Partner has no active device token")
                    tracker.save(update_fields=['initial_notified_at', 'fcm_failure_logged'])
                    initial_sent += 1
                else:
                    # No partner user assigned - alert owner immediately
                    cls._alert_owner_of_partner_unreachable(tracker.schedule, order, "No user account linked to assigned partner")
                    tracker.initial_notified_at = timezone.now()
                    tracker.save(update_fields=['initial_notified_at'])

            # Step 2: Reminder Notification (10 minutes after initial alert if unaccepted)
            elif now_dt >= reminder_notify_dt and tracker.initial_notified_at and not tracker.reminder_notified_at:
                if partner_user:
                    cls._send_partner_delivery_alert(
                        partner_user=partner_user,
                        order=order,
                        customer_name=customer_name,
                        time_str=time_str,
                        product_summary=product_summary,
                        is_reminder=True
                    )
                    tracker.reminder_notified_at = timezone.now()
                    tracker.save(update_fields=['reminder_notified_at'])
                    reminders_sent += 1

            # Step 3: Owner Escalation (20 minutes after initial alert if still unaccepted)
            elif now_dt >= owner_escalate_dt and tracker.reminder_notified_at and not tracker.owner_escalated_at:
                cls._alert_owner_of_unaccepted_order(tracker.schedule, order, time_str)
                tracker.owner_escalated_at = timezone.now()
                tracker.save(update_fields=['owner_escalated_at'])
                owner_escalations_sent += 1

        return {
            'timestamp': now_dt.isoformat(),
            'initial_sent': initial_sent,
            'reminders_sent': reminders_sent,
            'owner_escalations_sent': owner_escalations_sent,
        }

    @classmethod
    def _send_partner_delivery_alert(cls, partner_user: User, order: Order, customer_name: str, time_str: str, product_summary: str, is_reminder: bool = False) -> bool:
        """Dispatches FCM push to the fixed assigned partner."""
        prefix = "REMINDER: " if is_reminder else "New Scheduled Delivery: "
        title = f"{prefix}{customer_name} @ {time_str}"
        body = f"Deliver {product_summary}. Scheduled for {time_str}. Tap to accept."
        payload = {
            'type': 'scheduled_delivery_assigned',
            'order_id': str(order.id),
            'delivery_time': time_str,
            'customer_name': customer_name,
        }
        try:
            NotificationService.send_to_user(
                user=partner_user,
                title=title,
                body=body,
                data=payload
            )
            return True
        except Exception as e:
            logger.error(f"FCM send error for partner {partner_user.username}: {e}")
            return False

    @classmethod
    def _alert_owner_of_unaccepted_order(cls, schedule: DailyDeliverySchedule, order: Order, time_str: str):
        """Notifies Owner when assigned partner has not accepted 10 minutes prior to delivery."""
        partner_name = schedule.delivery_partner.name if schedule.delivery_partner else "Partner"
        customer_name = schedule.customer_profile.name if schedule.customer_profile else "Customer"
        title = f"Alert: Order #{order.id} Unaccepted"
        body = f"{partner_name} has not accepted scheduled delivery for {customer_name} ({time_str}). Please follow up."
        payload = {
            'type': 'order_unaccepted_escalation',
            'order_id': str(order.id),
            'schedule_id': str(schedule.id),
        }
        # Notify all active Owners
        owners = User.objects.filter(profile__role='owner', is_active=True)
        for owner in owners:
            NotificationService.send_to_user(
                user=owner,
                title=title,
                body=body,
                data=payload
            )

    @classmethod
    def _alert_owner_of_partner_unreachable(cls, schedule: DailyDeliverySchedule, order: Order, reason: str):
        """Notifies Owner if partner device token is missing or push notification fails."""
        partner_name = schedule.delivery_partner.name if schedule.delivery_partner else "Partner"
        customer_name = schedule.customer_profile.name if schedule.customer_profile else "Customer"
        title = f"FCM Warning: Delivery Partner Notification Issue"
        body = f"Could not send FCM alert to {partner_name} for {customer_name} (Order #{order.id}): {reason}."
        payload = {
            'type': 'fcm_failure_warning',
            'order_id': str(order.id),
            'schedule_id': str(schedule.id),
        }
        owners = User.objects.filter(profile__role='owner', is_active=True)
        for owner in owners:
            NotificationService.send_to_user(
                user=owner,
                title=title,
                body=body,
                data=payload
            )
