import zoneinfo
from decimal import Decimal
from datetime import date, time, datetime, timedelta
from django.test import TestCase
from django.utils import timezone
from django.contrib.auth.models import User
from accounts.models import UserProfile
from billing.models import Invoice, InvoiceItem, DraftInvoice, Payment, PaymentAllocation, Customer
from products.models import Product, Category
from notifications.models import DeviceToken, NotificationLog
from notifications.reminder_service import DailyBusinessReminderService
from orders.models import GlobalRecurringNotificationSetting

class DailyBusinessReminderTestCase(TestCase):
    def setUp(self):
        # 1. Setup Global Notification Settings
        self.settings = GlobalRecurringNotificationSetting.get_settings()
        self.settings.order_generation_time = time(6, 0, 0)
        self.settings.owner_notification_time = time(6, 5, 0)
        self.settings.partner_notification_time = time(6, 10, 0)
        self.settings.owner_notification_enabled = True
        self.settings.partner_notification_enabled = True
        self.settings.timezone = 'Asia/Kolkata'
        self.settings.save()

        # 2. Setup Owner user & profile
        self.owner = User.objects.create_user(
            username='test_owner',
            first_name='Owner',
            last_name='User'
        )
        self.owner_profile = UserProfile.objects.create(
            user=self.owner,
            phone='9999988888',
            role='owner'
        )
        self.owner_token = DeviceToken.objects.create(
            user=self.owner,
            token='fcm_token_owner_test',
            is_active=True
        )

        # 3. Setup Second Owner user & profile
        self.owner2 = User.objects.create_user(
            username='test_owner2',
            first_name='Owner2',
            last_name='User2'
        )
        self.owner2_profile = UserProfile.objects.create(
            user=self.owner2,
            phone='9999977777',
            role='owner'
        )
        self.owner2_token = DeviceToken.objects.create(
            user=self.owner2,
            token='fcm_token_owner2_test',
            is_active=True
        )

        # 4. Setup Customer & Product
        self.customer = Customer.objects.create(name='Acme Corp', phone='9876543210')
        self.category = Category.objects.create(name='Beverages')
        self.product = Product.objects.create(
            name='Masala Chai',
            price=Decimal('20.00'),
            category=self.category
        )

    # -------------------------------------------------------------
    # Scenario 1: Owner notifications enabled and Push Notifications enabled
    # -------------------------------------------------------------
    def test_01_owner_notifications_enabled_and_push_enabled(self):
        self.settings.owner_notification_enabled = True
        self.settings.save()
        self.assertTrue(self.owner_token.is_active)

        # Create 1 pending invoice
        Invoice.objects.create(
            invoice_number='INV-TEST-001',
            customer=self.customer,
            subtotal=Decimal('100.00'),
            grand_total=Decimal('100.00'),
            status='unpaid'
        )

        result = DailyBusinessReminderService.send_daily_reminder()
        self.assertTrue(result['sent'])
        self.assertEqual(result['dispatched_count'], 2)  # Dispatched to both active owners

        log = NotificationLog.objects.filter(recipient=self.owner).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, 'sent')
        self.assertEqual(log.channel, 'fcm')

    # -------------------------------------------------------------
    # Scenario 2: Owner notifications disabled
    # -------------------------------------------------------------
    def test_02_owner_notifications_disabled(self):
        self.settings.owner_notification_enabled = False
        self.settings.save()

        # Create 1 pending invoice
        Invoice.objects.create(
            invoice_number='INV-TEST-002',
            customer=self.customer,
            subtotal=Decimal('100.00'),
            grand_total=Decimal('100.00'),
            status='unpaid'
        )

        result = DailyBusinessReminderService.send_daily_reminder()
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'owner_notification_disabled')
        self.assertEqual(NotificationLog.objects.count(), 0)

    # -------------------------------------------------------------
    # Scenario 3: Push Notifications disabled (deactivated tokens)
    # -------------------------------------------------------------
    def test_03_push_notifications_disabled(self):
        self.settings.owner_notification_enabled = True
        self.settings.save()

        # Deactivate device tokens
        DeviceToken.objects.filter(user__in=[self.owner, self.owner2]).update(is_active=False)

        Invoice.objects.create(
            invoice_number='INV-TEST-003',
            customer=self.customer,
            subtotal=Decimal('100.00'),
            grand_total=Decimal('100.00'),
            status='unpaid'
        )

        result = DailyBusinessReminderService.send_daily_reminder()
        # Recorded in notification log inbox but marked push_disabled
        self.assertTrue(result['sent'])
        self.assertEqual(result['push_disabled_count'], 2)
        log = NotificationLog.objects.filter(recipient=self.owner).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, 'sent')

    # -------------------------------------------------------------
    # Scenario 4: Only pending invoices exist
    # -------------------------------------------------------------
    def test_04_only_pending_invoices_exist(self):
        Invoice.objects.create(
            invoice_number='INV-TEST-004',
            customer=self.customer,
            subtotal=Decimal('200.00'),
            grand_total=Decimal('200.00'),
            status='unpaid'
        )
        summary = DailyBusinessReminderService.get_owner_business_summary()
        self.assertEqual(summary['pending_payments_count'], 1)
        self.assertEqual(summary['partial_payments_count'], 0)
        self.assertEqual(summary['draft_bills_count'], 0)

        title, body, payload = DailyBusinessReminderService.build_notification_message(summary)
        self.assertEqual(payload['screen'], 'PendingPayments')
        self.assertEqual(payload['category'], 'pending_payments')
        self.assertIn('Pending Payments: 1', body)

    # -------------------------------------------------------------
    # Scenario 5: Only partial invoices exist
    # -------------------------------------------------------------
    def test_05_only_partial_invoices_exist(self):
        inv = Invoice.objects.create(
            invoice_number='INV-TEST-005',
            customer=self.customer,
            subtotal=Decimal('200.00'),
            grand_total=Decimal('200.00'),
            status='partially_paid'
        )
        pmt = Payment.objects.create(
            customer=self.customer,
            amount=Decimal('50.00'),
            method='cash'
        )
        PaymentAllocation.objects.create(
            payment=pmt,
            invoice=inv,
            allocated_amount=Decimal('50.00')
        )

        summary = DailyBusinessReminderService.get_owner_business_summary()
        self.assertEqual(summary['pending_payments_count'], 0)
        self.assertEqual(summary['partial_payments_count'], 1)
        self.assertEqual(summary['draft_bills_count'], 0)

        title, body, payload = DailyBusinessReminderService.build_notification_message(summary)
        self.assertEqual(payload['screen'], 'Invoices')
        self.assertEqual(payload['category'], 'partial_payments')
        self.assertIn('Partial Payments: 1', body)

    # -------------------------------------------------------------
    # Scenario 6: Only draft bills exist
    # -------------------------------------------------------------
    def test_06_only_draft_bills_exist(self):
        DraftInvoice.objects.create(
            draft_number='DRAFT-TEST-001',
            customer=self.customer,
            status='draft',
            subtotal=Decimal('80.00'),
            grand_total=Decimal('80.00')
        )
        summary = DailyBusinessReminderService.get_owner_business_summary()
        self.assertEqual(summary['pending_payments_count'], 0)
        self.assertEqual(summary['partial_payments_count'], 0)
        self.assertEqual(summary['draft_bills_count'], 1)

        title, body, payload = DailyBusinessReminderService.build_notification_message(summary)
        self.assertEqual(payload['screen'], 'QuickBilling')
        self.assertEqual(payload['category'], 'draft_bills')
        self.assertIn('Draft Bills: 1', body)

    # -------------------------------------------------------------
    # Scenario 7: All three categories exist
    # -------------------------------------------------------------
    def test_07_all_three_categories_exist(self):
        # 1. Pending invoice
        Invoice.objects.create(
            invoice_number='INV-TEST-007A',
            customer=self.customer,
            grand_total=Decimal('100.00'),
            status='unpaid'
        )

        # 2. Partial invoice
        inv2 = Invoice.objects.create(
            invoice_number='INV-TEST-007B',
            customer=self.customer,
            grand_total=Decimal('150.00'),
            status='partially_paid'
        )
        pmt = Payment.objects.create(customer=self.customer, amount=Decimal('50.00'), method='cash')
        PaymentAllocation.objects.create(payment=pmt, invoice=inv2, allocated_amount=Decimal('50.00'))

        # 3. Draft bill
        DraftInvoice.objects.create(
            draft_number='DRAFT-TEST-007',
            customer=self.customer,
            status='draft'
        )

        summary = DailyBusinessReminderService.get_owner_business_summary()
        self.assertEqual(summary['pending_payments_count'], 1)
        self.assertEqual(summary['partial_payments_count'], 1)
        self.assertEqual(summary['draft_bills_count'], 1)
        self.assertEqual(summary['total_alerts'], 3)

        result = DailyBusinessReminderService.send_daily_reminder()
        self.assertTrue(result['sent'])
        self.assertEqual(result['dispatched_count'], 2)

    # -------------------------------------------------------------
    # Scenario 8: All categories are empty (Zero activity suppression)
    # -------------------------------------------------------------
    def test_08_all_categories_are_empty(self):
        summary = DailyBusinessReminderService.get_owner_business_summary()
        self.assertEqual(summary['total_alerts'], 0)

        result = DailyBusinessReminderService.send_daily_reminder(force=False)
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'all_zero')
        self.assertEqual(NotificationLog.objects.count(), 0)

    # -------------------------------------------------------------
    # Scenario 9: Payment completed before next scheduled reminder
    # -------------------------------------------------------------
    def test_09_payment_completed_before_next_reminder(self):
        inv = Invoice.objects.create(
            invoice_number='INV-TEST-009',
            customer=self.customer,
            grand_total=Decimal('200.00'),
            status='unpaid'
        )
        self.assertEqual(DailyBusinessReminderService.get_owner_business_summary()['pending_payments_count'], 1)

        # Pay in full
        pmt = Payment.objects.create(customer=self.customer, amount=Decimal('200.00'), method='cash')
        PaymentAllocation.objects.create(payment=pmt, invoice=inv, allocated_amount=Decimal('200.00'))

        summary = DailyBusinessReminderService.get_owner_business_summary()
        self.assertEqual(summary['pending_payments_count'], 0)
        self.assertEqual(summary['partial_payments_count'], 0)
        self.assertEqual(summary['total_alerts'], 0)

        result = DailyBusinessReminderService.send_daily_reminder()
        self.assertFalse(result['sent'])

    # -------------------------------------------------------------
    # Scenario 10: Draft finalized before next reminder
    # -------------------------------------------------------------
    def test_10_draft_finalized_before_next_reminder(self):
        draft = DraftInvoice.objects.create(
            draft_number='DRAFT-TEST-010',
            customer=self.customer,
            status='draft'
        )
        self.assertEqual(DailyBusinessReminderService.get_owner_business_summary()['draft_bills_count'], 1)

        # Finalize draft
        draft.status = 'finalized'
        draft.save()

        summary = DailyBusinessReminderService.get_owner_business_summary()
        self.assertEqual(summary['draft_bills_count'], 0)

    # -------------------------------------------------------------
    # Scenario 11: Owner changes notification time
    # -------------------------------------------------------------
    def test_11_owner_changes_notification_time(self):
        # Initial saved time: 06:05:00
        self.assertEqual(self.settings.owner_notification_time, time(6, 5, 0))

        # Update saved time in Settings to 08:30:00
        self.settings.owner_notification_time = time(8, 30, 0)
        self.settings.save()

        tz = DailyBusinessReminderService.get_effective_timezone()
        # Simulated test time at 07:00:00 (after 06:05:00, but before 08:30:00)
        test_dt_before = datetime(2026, 11, 23, 7, 0, 0, tzinfo=tz)
        self.assertFalse(DailyBusinessReminderService.is_scheduled_time_reached(current_dt=test_dt_before))

        # Simulated test time at 08:35:00 (after 08:30:00)
        test_dt_after = datetime(2026, 11, 23, 8, 35, 0, tzinfo=tz)
        self.assertTrue(DailyBusinessReminderService.is_scheduled_time_reached(current_dt=test_dt_after))

    # -------------------------------------------------------------
    # Scenario 12: Partner notification settings do not affect owner
    # -------------------------------------------------------------
    def test_12_partner_notification_settings_do_not_affect_owner(self):
        # Disable partner notifications and change partner time
        self.settings.partner_notification_enabled = False
        self.settings.partner_notification_time = time(10, 0, 0)
        self.settings.owner_notification_enabled = True
        self.settings.save()

        # Create pending invoice
        Invoice.objects.create(
            invoice_number='INV-TEST-012',
            customer=self.customer,
            grand_total=Decimal('50.00'),
            status='unpaid'
        )

        result = DailyBusinessReminderService.send_daily_reminder()
        self.assertTrue(result['sent'])
        self.assertEqual(result['dispatched_count'], 2)

    # -------------------------------------------------------------
    # Scenario 13: Duplicate prevention works when scheduler runs twice
    # -------------------------------------------------------------
    def test_13_duplicate_prevention_on_second_run(self):
        Invoice.objects.create(
            invoice_number='INV-TEST-013',
            customer=self.customer,
            grand_total=Decimal('100.00'),
            status='unpaid'
        )

        first_run = DailyBusinessReminderService.send_daily_reminder()
        self.assertTrue(first_run['sent'])
        self.assertEqual(first_run['dispatched_count'], 2)

        # Immediate second run
        second_run = DailyBusinessReminderService.send_daily_reminder()
        self.assertFalse(second_run['sent'])
        self.assertEqual(second_run['skipped_count'], 2)
        self.assertEqual(second_run['dispatched_count'], 0)

    # -------------------------------------------------------------
    # Scenario 14: Mobile app closed (Server background scheduler execution)
    # -------------------------------------------------------------
    def test_14_mobile_app_closed_background_execution(self):
        """
        When the mobile app is closed, the backend scheduler command triggers autonomously,
        recording FCM payloads in NotificationLog with active device tokens.
        """
        Invoice.objects.create(
            invoice_number='INV-TEST-014',
            customer=self.customer,
            grand_total=Decimal('100.00'),
            status='unpaid'
        )

        # Command executed headlessly
        result = DailyBusinessReminderService.send_daily_reminder()
        self.assertTrue(result['sent'])

        logs = NotificationLog.objects.filter(data_payload__type='daily_business_reminder')
        self.assertEqual(logs.count(), 2)
        for l in logs:
            self.assertEqual(l.status, 'sent')
            self.assertEqual(l.channel, 'fcm')
            self.assertIn('daily_business_reminder', l.data_payload['type'])
