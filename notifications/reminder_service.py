import logging
from decimal import Decimal
from datetime import date, time, datetime, timedelta
import zoneinfo
from django.db.models import Sum, Q
from django.utils import timezone
from django.contrib.auth.models import User
from billing.models import Invoice, DraftInvoice
from notifications.models import NotificationLog, DeviceToken
from notifications.services import NotificationService
from orders.models import GlobalRecurringNotificationSetting

logger = logging.getLogger(__name__)

class DailyBusinessReminderService:
    """
    Automated Daily Business Reminder Service for Owners.
    Integrated with Global Recurring Notification Settings (Singleton):
    - order_generation_time
    - owner_notification_time (Saved Owner reminder schedule)
    - partner_notification_time
    - owner_notification_enabled (Master toggle for owner notifications)
    - partner_notification_enabled
    - timezone (Default 'Asia/Kolkata')

    Gathers:
    1. Pending Payments (finalized, grand_total > 0, total_paid == 0)
    2. Partial Payments (finalized, total_paid > 0, pending_amount > 0)
    3. Unfinalized Draft Bills (status='draft')

    Consolidates counts into one push notification per Owner per day.
    Prevents duplicate dispatches for the same date and owner.
    """

    @classmethod
    def get_global_settings(cls):
        """Fetches the singleton GlobalRecurringNotificationSetting."""
        return GlobalRecurringNotificationSetting.get_settings()

    @classmethod
    def get_owner_business_summary(cls):
        """
        Calculates:
        - pending_payments_count: Invoices with total_paid == 0 and pending_amount > 0
        - partial_payments_count: Invoices with total_paid > 0 and pending_amount > 0
        - draft_bills_count: DraftInvoices with status='draft'
        """
        # Active finalized invoices (exclude void/cancelled)
        active_invoices = Invoice.objects.exclude(status='void')

        pending_payments_count = 0
        partial_payments_count = 0

        for inv in active_invoices:
            # Source of truth: invoice.total_paid and pending_amount
            paid = inv.total_paid
            pending = inv.pending_amount
            if pending > Decimal('0.00'):
                if paid == Decimal('0.00'):
                    pending_payments_count += 1
                else:
                    partial_payments_count += 1

        # Draft bills (exclude finalized or cancelled)
        draft_bills_count = DraftInvoice.objects.filter(status='draft').count()

        return {
            'pending_payments_count': pending_payments_count,
            'partial_payments_count': partial_payments_count,
            'draft_bills_count': draft_bills_count,
            'total_alerts': pending_payments_count + partial_payments_count + draft_bills_count,
        }

    @classmethod
    def get_effective_timezone(cls, global_settings=None):
        """Returns the configured zoneinfo.ZoneInfo instance."""
        settings_obj = global_settings or cls.get_global_settings()
        tz_str = getattr(settings_obj, 'timezone', 'Asia/Kolkata') or 'Asia/Kolkata'
        try:
            return zoneinfo.ZoneInfo(tz_str)
        except Exception:
            return zoneinfo.ZoneInfo('Asia/Kolkata')

    @classmethod
    def has_owner_received_reminder_today(cls, owner_user: User, check_date: date = None) -> bool:
        """
        Checks if the owner has already received a daily business reminder for the specified date.
        Uses the scheduling timezone (Asia/Kolkata) to ensure date idempotency.
        """
        if check_date is None:
            tz = cls.get_effective_timezone()
            check_date = timezone.localtime(timezone.now(), tz).date()

        date_str = check_date.isoformat()

        return NotificationLog.objects.filter(
            recipient=owner_user,
            data_payload__type='daily_business_reminder',
            data_payload__reminder_date=date_str,
            status__in=['sent', 'pending']
        ).exists()

    @classmethod
    def build_notification_message(cls, summary: dict, check_date: date = None) -> tuple:
        """
        Builds title, body, and payload for the daily reminder.
        """
        title = "Chai Flow — Daily Business Reminder"
        body = (
            f"• Pending Payments: {summary['pending_payments_count']}\n"
            f"• Partial Payments: {summary['partial_payments_count']}\n"
            f"• Draft Bills: {summary['draft_bills_count']}"
        )

        # Primary navigation screen recommendation
        # Priority: Pending Payments -> PendingPayments, Partial Payments -> Invoices, Draft Bills -> QuickBilling
        primary_screen = "PendingPayments"
        primary_category = "pending_payments"
        if summary['pending_payments_count'] > 0:
            primary_screen = "PendingPayments"
            primary_category = "pending_payments"
        elif summary['partial_payments_count'] > 0:
            primary_screen = "Invoices"
            primary_category = "partial_payments"
        elif summary['draft_bills_count'] > 0:
            primary_screen = "QuickBilling"
            primary_category = "draft_bills"

        if check_date is None:
            tz = cls.get_effective_timezone()
            check_date = timezone.localtime(timezone.now(), tz).date()

        date_str = check_date.isoformat()

        payload = {
            'type': 'daily_business_reminder',
            'screen': primary_screen,
            'category': primary_category,
            'pending_count': str(summary['pending_payments_count']),
            'partial_count': str(summary['partial_payments_count']),
            'draft_count': str(summary['draft_bills_count']),
            'reminder_date': date_str,
        }

        return title, body, payload

    @classmethod
    def is_scheduled_time_reached(cls, current_dt: datetime = None, window_minutes: int = 15) -> bool:
        """
        Checks if current time in configured timezone has reached or passed the saved owner_notification_time.
        Used by recurring schedulers.
        """
        settings_obj = cls.get_global_settings()
        tz = cls.get_effective_timezone(settings_obj)

        if current_dt is None:
            current_dt = timezone.localtime(timezone.now(), tz)

        scheduled_time = settings_obj.owner_notification_time
        # Create a datetime on current date with scheduled_time
        scheduled_dt = datetime.combine(current_dt.date(), scheduled_time).replace(tzinfo=tz)

        # If current_dt >= scheduled_dt and within window_minutes (or if run on a daily trigger)
        diff_seconds = (current_dt - scheduled_dt).total_seconds()
        return diff_seconds >= 0

    @classmethod
    def send_daily_reminder(cls, force: bool = False, check_time: bool = False, window_minutes: int = 15) -> dict:
        """
        Executes daily reminder check for all owners respecting:
        1. Global owner_notification_enabled switch.
        2. Saved owner_notification_time (when check_time=True).
        3. Push notifications master availability (active DeviceTokens).
        4. Zero-count suppression: skips if all 3 categories have 0 counts (unless force=True).
        5. Duplicate prevention: skips if owner already received reminder today.
        6. Independent handling for multiple owners.
        """
        settings_obj = cls.get_global_settings()
        tz = cls.get_effective_timezone(settings_obj)
        now_local = timezone.localtime(timezone.now(), tz)
        today_date = now_local.date()

        # Rule 3: Respect global owner_notification_enabled
        if not settings_obj.owner_notification_enabled and not force:
            logger.info("[Daily Business Reminder] Owner notifications disabled in Global Settings. Skipping.")
            return {
                'sent': False,
                'reason': 'owner_notification_disabled',
                'summary': None,
                'recipients': 0,
            }

        # Rule 2: If check_time is requested (e.g. periodic poller), verify scheduled time is reached
        if check_time and not force:
            scheduled_time = settings_obj.owner_notification_time
            scheduled_dt = datetime.combine(today_date, scheduled_time).replace(tzinfo=tz)
            if now_local < scheduled_dt:
                logger.info(
                    f"[Daily Business Reminder] Scheduled time {scheduled_time} not reached yet (current: {now_local.time()}). Skipping."
                )
                return {
                    'sent': False,
                    'reason': 'time_not_reached',
                    'scheduled_time': str(scheduled_time),
                    'current_time': str(now_local.time()),
                }

        summary = cls.get_owner_business_summary()

        logger.info(
            f"[Daily Business Reminder] Summary: Pending={summary['pending_payments_count']}, "
            f"Partial={summary['partial_payments_count']}, Drafts={summary['draft_bills_count']}"
        )

        # Rule 4: If all three categories have zero records, do not send an unnecessary notification.
        if summary['total_alerts'] == 0 and not force:
            logger.info("[Daily Business Reminder] All categories zero. Skipping notification dispatch.")
            return {
                'sent': False,
                'reason': 'all_zero',
                'summary': summary,
                'recipients': 0,
            }

        # Identify all active owners
        owner_users = list(
            User.objects.filter(
                Q(profile__role='owner') | Q(is_staff=True) | Q(is_superuser=True),
                is_active=True
            ).distinct()
        )

        if not owner_users:
            logger.warning("[Daily Business Reminder] No active business owners found in database.")
            return {
                'sent': False,
                'reason': 'no_owners',
                'summary': summary,
                'recipients': 0,
            }

        title, body, payload = cls.build_notification_message(summary, check_date=today_date)
        dispatched_count = 0
        skipped_count = 0
        push_disabled_count = 0

        for owner in owner_users:
            # Check duplicate prevention
            if not force and cls.has_owner_received_reminder_today(owner, today_date):
                logger.info(f"[Daily Business Reminder] Owner {owner.username} already received reminder for {today_date}. Skipping.")
                skipped_count += 1
                continue

            # Check if owner has active FCM push tokens registered
            has_active_push = DeviceToken.objects.filter(user=owner, is_active=True).exists()

            NotificationService.send_to_user(
                user=owner,
                title=title,
                body=body,
                data=payload,
            )
            dispatched_count += 1
            if not has_active_push:
                push_disabled_count += 1
            logger.info(f"[Daily Business Reminder] Dispatched reminder to owner {owner.username} (active_push={has_active_push}).")

        return {
            'sent': dispatched_count > 0,
            'summary': summary,
            'dispatched_count': dispatched_count,
            'skipped_count': skipped_count,
            'push_disabled_count': push_disabled_count,
            'total_owners': len(owner_users),
            'reminder_date': today_date.isoformat(),
            'scheduled_time': str(settings_obj.owner_notification_time),
            'timezone': str(tz),
        }
