from django.core.management.base import BaseCommand
from orders.daily_schedule_service import DailyScheduleService

class Command(BaseCommand):
    help = "Evaluates scheduled orders and dispatches partner initial FCM pushes, 10-min reminders, and owner escalations."

    def handle(self, *args, **options):
        res = DailyScheduleService.process_notification_reminders()
        self.stdout.write(
            self.style.SUCCESS(
                f"Notification Scheduler completed at {res['timestamp']}: "
                f"Initial alerts: {res['initial_sent']}, Reminders: {res['reminders_sent']}, "
                f"Owner escalations: {res['owner_escalations_sent']}."
            )
        )
