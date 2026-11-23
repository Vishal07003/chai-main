import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from notifications.reminder_service import DailyBusinessReminderService

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = (
        "Send daily business reminder push notification to Business Owners "
        "using saved Owner Notification Time from Global Recurring Notification Settings. "
        "Summarizes pending payments, partial payments, and unfinalized draft bills."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force sending even if already sent today, outside scheduled time, or counts are zero',
        )
        parser.add_argument(
            '--check-time',
            action='store_true',
            help='Only send if current time has reached or passed saved owner_notification_time',
        )

    def handle(self, *args, **options):
        force = options.get('force', False)
        check_time = options.get('check_time', False)
        now_local = timezone.localtime(timezone.now(), DailyBusinessReminderService.get_effective_timezone())

        self.stdout.write(
            self.style.NOTICE(f"[{now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}] Running Daily Business Reminder...")
        )

        result = DailyBusinessReminderService.send_daily_reminder(force=force, check_time=check_time)

        if result.get('sent'):
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully sent daily business reminder to {result.get('dispatched_count')} owner(s). "
                    f"(Skipped {result.get('skipped_count')} already notified today). "
                    f"Summary: Pending={result['summary']['pending_payments_count']}, "
                    f"Partial={result['summary']['partial_payments_count']}, "
                    f"Drafts={result['summary']['draft_bills_count']}"
                )
            )
        else:
            reason = result.get('reason')
            if reason == 'owner_notification_disabled':
                self.stdout.write(
                    self.style.WARNING("Owner notifications are disabled in Global Settings. Notification skipped.")
                )
            elif reason == 'time_not_reached':
                self.stdout.write(
                    self.style.WARNING(
                        f"Saved Owner Notification Time ({result.get('scheduled_time')}) not reached yet. "
                        f"Current time: {result.get('current_time')}."
                    )
                )
            elif reason == 'all_zero':
                self.stdout.write(
                    self.style.WARNING("All categories (pending, partial, drafts) are zero. Notification skipped.")
                )
            elif reason == 'no_owners':
                self.stdout.write(
                    self.style.ERROR("No active business owners found to notify.")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f"No reminders sent (All {result.get('skipped_count')} owners already notified today).")
                )
