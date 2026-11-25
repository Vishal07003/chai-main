from datetime import date
from django.core.management.base import BaseCommand
from orders.daily_schedule_service import DailyScheduleService

class Command(BaseCommand):
    help = "Generates daily orders for active customer schedules for a specified date (defaults to today)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--date',
            type=str,
            help="Target date in YYYY-MM-DD format (default: today)",
            default=None
        )

    def handle(self, *args, **options):
        date_str = options.get('date')
        target_date = None
        if date_str:
            try:
                target_date = date.fromisoformat(date_str)
            except ValueError:
                self.stderr.write(self.style.ERROR(f"Invalid date format '{date_str}'. Expected YYYY-MM-DD."))
                return

        res = DailyScheduleService.generate_orders_for_date(target_date)
        self.stdout.write(
            self.style.SUCCESS(
                f"Generated {res['generated_count']} scheduled order(s) for {res['target_date']}. "
                f"Already generated: {res['skipped_existing_count']}, Ineligible: {res['skipped_ineligible_count']}."
            )
        )
