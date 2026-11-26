import os
import logging
import urllib.request
import urllib.parse
import json
from datetime import datetime
from django.conf import settings
from django.db import transaction, models
from django.utils import timezone
from .models import DeviceToken, NotificationLog

logger = logging.getLogger(__name__)

# Initialize Firebase Admin securely
_firebase_initialized = False

def initialize_firebase():
    global _firebase_initialized
    if _firebase_initialized:
        return True

    try:
        import firebase_admin
        from firebase_admin import credentials

        cred_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', None) or os.getenv('FIREBASE_CREDENTIALS_PATH')
        
        if cred_path and os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
            _firebase_initialized = True
            logger.info("Firebase Admin SDK initialized successfully with service account.")
            return True
        else:
            logger.warning("FIREBASE_CREDENTIALS_PATH not set or file not found. Push notifications will run in mock/log mode.")
            return False
    except Exception as e:
        logger.error(f"Failed to initialize Firebase Admin SDK: {e}")
        return False


class NotificationService:
    """
    Centralized Phase 4 Notification Engine:
    - Supported channels: FCM (Firebase Cloud Messaging) and SMS (via backend provider).
    - EXPLICITLY EXCLUDES WhatsApp (no WABA/Meta/WhatsApp endpoints or webhooks).
    - Centralized delivery history, status tracking, and retry mechanism.
    - Role-based dispatch for Owner and Delivery Partner.
    """

    # -------------------------------------------------------------
    # 1. SMS Dispatch Engine (Reusing 2Factor.in SMS Integration)
    # -------------------------------------------------------------
    @classmethod
    def send_sms(cls, phone: str, message: str, recipient=None, target_role: str = 'all', log_entry=None) -> bool:
        """
        Sends an operational SMS notification via backend 2Factor provider integration.
        Excludes WhatsApp. Logs delivery status and retry attempts.
        """
        from accounts.otp_service import OtpProviderService

        clean_mobile = OtpProviderService.clean_phone(phone)
        if not clean_mobile or len(clean_mobile) != 10:
            logger.error(f"[SMS Notification] Invalid phone: {phone}")
            return False

        if not log_entry:
            log_entry = NotificationLog.objects.create(
                recipient=recipient,
                target_role=target_role,
                channel='sms',
                title='SMS Alert',
                body=message,
                data_payload={'phone': clean_mobile},
                status='pending',
            )

        api_key = OtpProviderService.get_api_key()
        base_url = OtpProviderService.get_provider_base_url()
        is_mock = OtpProviderService.is_mock_mode()

        if is_mock:
            log_entry.status = 'sent'
            log_entry.sent_at = timezone.now()
            log_entry.save(update_fields=['status', 'sent_at'])
            logger.info(f"[SMS Mock Send] To: +91{clean_mobile} | Message: {message}")
            return True

        # Live 2Factor Transactional SMS endpoint
        # Uses ADDON_SERVICES/SEND/TSMS or direct SMS endpoint
        try:
            template = getattr(settings, 'OTP_SMS_TEMPLATE', '') or os.getenv('OTP_SMS_TEMPLATE', '')
            tsms_template = getattr(settings, 'SMS_TRANSACTIONAL_TEMPLATE', '') or os.getenv('SMS_TRANSACTIONAL_TEMPLATE', '')

            if tsms_template:
                # 2Factor Transactional Open Template SMS POST endpoint
                url = f"{base_url}/{api_key}/ADDON_SERVICES/SEND/TSMS"
                post_data = urllib.parse.urlencode({
                    'From': getattr(settings, 'SMS_SENDER_ID', 'CHAIFL'),
                    'To': clean_mobile,
                    'TemplateName': tsms_template,
                    'VAR1': message,
                }).encode('utf-8')
                req = urllib.request.Request(
                    url,
                    data=post_data,
                    headers={'User-Agent': 'ChaiFlow-Backend/1.0', 'Accept': 'application/json'},
                    method='POST',
                )
            elif template:
                # Fallback to configured SMS OTP template route if available
                url = f"{base_url}/{api_key}/SMS/+91{clean_mobile}/{urllib.parse.quote(message)}/{urllib.parse.quote(template)}"
                req = urllib.request.Request(
                    url,
                    headers={'User-Agent': 'ChaiFlow-Backend/1.0', 'Accept': 'application/json'},
                    method='GET',
                )
            else:
                # When no DLT transactional template is configured on 2Factor,
                # standard SMS route /SMS/+91{mobile}/{msg} fails with 400 because 2Factor expects an OTP code (4-6 chars).
                # Log a descriptive warning and record mock dispatch to prevent crashing delivery workflows.
                logger.warning(
                    f"[SMS Notification] No SMS_TRANSACTIONAL_TEMPLATE or OTP_SMS_TEMPLATE configured for 2Factor. "
                    f"2Factor /SMS endpoint requires numeric OTP code. Skipping external dispatch for operational message."
                )
                log_entry.status = 'sent'
                log_entry.sent_at = timezone.now()
                log_entry.error_message = 'Simulated delivery: No DLT transactional template configured for non-OTP message.'
                log_entry.save(update_fields=['status', 'sent_at', 'error_message'])
                return True

            with urllib.request.urlopen(req, timeout=10) as resp:
                res_data = json.loads(resp.read().decode('utf-8'))

            if res_data.get('Status') == 'Success':
                log_entry.status = 'sent'
                log_entry.sent_at = timezone.now()
                log_entry.error_message = None
                log_entry.save(update_fields=['status', 'sent_at', 'error_message'])
                return True
            else:
                err_detail = str(res_data.get('Details', 'SMS provider submission error'))
                log_entry.status = 'failed'
                log_entry.error_message = err_detail
                log_entry.save(update_fields=['status', 'error_message'])
                logger.error(f"[SMS Send Failed] {err_detail}")
                return False

        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode('utf-8')
                err_data = json.loads(err_body)
                err_msg = err_data.get('Details') or err_data.get('message') or f"HTTP {e.code}: {e.reason} - {err_body}"
            except Exception:
                err_msg = f"HTTP {e.code}: {e.reason}"
            log_entry.status = 'failed'
            log_entry.error_message = err_msg
            log_entry.save(update_fields=['status', 'error_message'])
            logger.error(f"[SMS Send HTTPError] {err_msg}")
            return False
        except Exception as e:
            log_entry.status = 'failed'
            log_entry.error_message = str(e)
            log_entry.save(update_fields=['status', 'error_message'])
            logger.error(f"[SMS Send Exception] {e}")
            return False

    # -------------------------------------------------------------
    # 2. FCM Push Notification Engine
    # -------------------------------------------------------------
    @classmethod
    def send_to_user(cls, user, title: str, body: str, data: dict = None, log_entry=None):
        """
        Send FCM notification to all active devices registered to a specific user.
        Records delivery history in NotificationLog.
        """
        role = getattr(user.profile, 'role', 'customer') if hasattr(user, 'profile') else 'customer'
        if not log_entry:
            log_entry = NotificationLog.objects.create(
                recipient=user,
                target_role=role,
                channel='fcm',
                title=title,
                body=body,
                data_payload=data or {},
                status='pending',
            )

        active_tokens = list(
            DeviceToken.objects.filter(user=user, is_active=True).values_list('token', flat=True)
        )
        if not active_tokens:
            log_entry.status = 'sent'  # Queued for user inbox
            log_entry.sent_at = timezone.now()
            log_entry.save(update_fields=['status', 'sent_at'])
            return

        cls.send_multicast(active_tokens, title, body, data, log_entry=log_entry)

    @classmethod
    def send_to_role(cls, role: str, title: str, body: str, data: dict = None, log_entry=None):
        """
        Send FCM notification to all active devices of users with a specific role ('owner' or 'delivery').
        """
        if not log_entry:
            log_entry = NotificationLog.objects.create(
                recipient=None,
                target_role=role,
                channel='fcm',
                title=title,
                body=body,
                data_payload=data or {},
                status='pending',
            )

        active_tokens = list(
            DeviceToken.objects.filter(
                user__profile__role=role,
                is_active=True
            ).values_list('token', flat=True)
        )
        if not active_tokens:
            log_entry.status = 'sent'
            log_entry.sent_at = timezone.now()
            log_entry.save(update_fields=['status', 'sent_at'])
            return

        cls.send_multicast(active_tokens, title, body, data, log_entry=log_entry)

    @classmethod
    def send_multicast(cls, tokens: list, title: str, body: str, data: dict = None, log_entry=None):
        if not tokens:
            return

        data_payload = {k: str(v) for k, v in (data or {}).items()}

        if not initialize_firebase():
            logger.info(f"[FCM Mock Send] Title: {title} | Body: {body} | Recipients: {len(tokens)}")
            if log_entry:
                log_entry.status = 'sent'
                log_entry.sent_at = timezone.now()
                log_entry.save(update_fields=['status', 'sent_at'])
            return

        try:
            from firebase_admin import messaging

            message = messaging.MulticastMessage(
                notification=messaging.Notification(title=title, body=body),
                data=data_payload,
                tokens=tokens,
            )
            response = messaging.send_multicast(message)

            if log_entry:
                if response.failure_count == len(tokens):
                    log_entry.status = 'failed'
                    log_entry.error_message = f"All {len(tokens)} token deliveries failed."
                else:
                    log_entry.status = 'sent'
                    log_entry.sent_at = timezone.now()
                log_entry.save(update_fields=['status', 'sent_at', 'error_message'])

            # Handle failed / invalid registration tokens
            if response.failure_count > 0:
                invalid_tokens = []
                for idx, resp in enumerate(response.responses):
                    if not resp.success:
                        err_code = getattr(resp.exception, 'code', '')
                        if err_code == 'registration-token-not-registered':
                            invalid_tokens.append(tokens[idx])

                if invalid_tokens:
                    DeviceToken.objects.filter(token__in=invalid_tokens).update(is_active=False)
                    logger.info(f"Deactivated {len(invalid_tokens)} invalid FCM device tokens.")

        except Exception as e:
            logger.error(f"Error dispatching FCM multicast message: {e}")
            if log_entry:
                log_entry.status = 'failed'
                log_entry.error_message = str(e)
                log_entry.save(update_fields=['status', 'error_message'])

    # -------------------------------------------------------------
    # 3. Retry Mechanism for Failed Notifications
    # -------------------------------------------------------------
    @classmethod
    def retry_failed_notifications(cls, limit: int = 20) -> int:
        """
        Retries up to `limit` failed notifications that have not exceeded max_retries.
        """
        failed_entries = NotificationLog.objects.filter(
            status='failed',
            retry_count__lt=models.F('max_retries')
        )[:limit]

        retried_count = 0
        for entry in failed_entries:
            entry.retry_count += 1
            entry.save(update_fields=['retry_count'])

            if entry.channel == 'fcm':
                if entry.recipient:
                    cls.send_to_user(entry.recipient, entry.title, entry.body, entry.data_payload, log_entry=entry)
                elif entry.target_role:
                    cls.send_to_role(entry.target_role, entry.title, entry.body, entry.data_payload, log_entry=entry)
                retried_count += 1
            elif entry.channel == 'sms':
                phone = entry.data_payload.get('phone')
                if phone:
                    cls.send_sms(phone, entry.body, recipient=entry.recipient, target_role=entry.target_role, log_entry=entry)
                    retried_count += 1

        return retried_count

    # -------------------------------------------------------------
    # 4. High-Level Role-Based Domain Event Handlers
    # -------------------------------------------------------------
    @classmethod
    def _is_duplicate_event(cls, dedup_key: str) -> bool:
        """
        Check if a notification event with this deduplication key has already been logged.
        """
        if not dedup_key:
            return False
        return NotificationLog.objects.filter(data_payload__dedup_key=dedup_key).exists()

    @classmethod
    def on_order_created(cls, order):
        """1. New customer order -> Notify Business Owners (FCM)"""
        dedup_key = f"order_created_{order.id}"

        def dispatch():
            if cls._is_duplicate_event(dedup_key):
                logger.info(f"[NotificationService] Skipping duplicate order_created for #{order.id}")
                return

            cust_name = getattr(order.customer_profile, 'name', None) or order.customer.get_full_name() or order.customer.username
            order_num = f"ORD-{order.id:04d}"
            address = order.delivery_address or 'Counter Pickup'
            total_str = f"₹{order.total_amount}"

            cls.send_to_role(
                role='owner',
                title="New Order Received! ☕",
                body=f"{order_num} for {total_str} from {cust_name} ({address}).",
                data={
                    'type': 'new_order',
                    'order_id': order.id,
                    'order_number': order_num,
                    'customer_name': cust_name,
                    'delivery_address': address,
                    'total_amount': str(order.total_amount),
                    'screen': 'Orders',
                    'dedup_key': dedup_key,
                }
            )

        transaction.on_commit(dispatch)

    @classmethod
    def on_order_status_updated(cls, order):
        """2. Order status progression -> Notify Customer & Owner (FCM)"""
        status_messages = {
            'accepted': ("Order Accepted ✅", f"Your order #{order.id} has been accepted by the tea stall."),
            'preparing': ("Brewing Your Chai ☕", f"Your order #{order.id} is currently being prepared fresh!"),
            'out_for_delivery': ("Out for Delivery 🚴", f"Order #{order.id} is on its way to you!"),
            'delivered': ("Order Delivered 🎉", f"Order #{order.id} has been delivered. Enjoy your chai!"),
            'cancelled': ("Order Cancelled", f"Order #{order.id} has been cancelled."),
        }

        msg = status_messages.get(order.status)
        if msg:
            title, body = msg
            dedup_key = f"order_status_{order.id}_{order.status}"

            def dispatch():
                if cls._is_duplicate_event(dedup_key):
                    return
                cls.send_to_user(
                    user=order.customer,
                    title=title,
                    body=body,
                    data={
                        'type': 'order_status',
                        'order_id': order.id,
                        'status': order.status,
                        'screen': 'Orders',
                        'dedup_key': dedup_key,
                    }
                )

            transaction.on_commit(dispatch)

    @classmethod
    def on_delivery_assigned(cls, delivery):
        """3. Delivery assigned -> Notify Delivery Partner (FCM & SMS alert)"""
        if not delivery.driver:
            return

        driver_user = delivery.driver
        delivery_id = delivery.id
        order = delivery.order
        dedup_key = f"delivery_assigned_{delivery_id}_{driver_user.id}"

        def dispatch():
            if cls._is_duplicate_event(dedup_key):
                logger.info(f"[NotificationService] Skipping duplicate delivery_assigned for delivery #{delivery_id}")
                return

            cust_name = getattr(order.customer_profile, 'name', None) or order.customer.get_full_name() or order.customer.username
            order_num = f"ORD-{order.id:04d}"
            address = delivery.order.delivery_address or 'Local delivery'

            # Items summary for partner
            items_summary = ", ".join([f"{it.quantity}x {it.product.name}" for it in order.items.select_related('product')[:3]])

            body_text = f"{order_num} for {cust_name}: Deliver to {address}. ({items_summary})"

            cls.send_to_user(
                user=driver_user,
                title="New Delivery Assigned 🚴",
                body=body_text,
                data={
                    'type': 'delivery_assigned',
                    'delivery_id': delivery_id,
                    'order_id': order.id,
                    'order_number': order_num,
                    'customer_name': cust_name,
                    'delivery_address': address,
                    'screen': 'OrderDetails',
                    'dedup_key': dedup_key,
                }
            )

            # Also dispatch SMS alert to driver if phone exists
            driver_phone = getattr(getattr(driver_user, 'profile', None), 'phone', '')
            if driver_phone:
                cls.send_sms(
                    phone=driver_phone,
                    message=f"ChaiFlow: Assigned {order_num} for {cust_name}. Deliver to {address}.",
                    recipient=driver_user,
                    target_role='delivery',
                )

        transaction.on_commit(dispatch)

    @classmethod
    def on_delivery_completed(cls, delivery):
        """4. Delivery marked completed -> Notify Owner"""
        delivery_id = delivery.id
        order = delivery.order
        dedup_key = f"delivery_completed_{delivery_id}"

        def dispatch():
            if cls._is_duplicate_event(dedup_key):
                logger.info(f"[NotificationService] Skipping duplicate delivery_completed for delivery #{delivery_id}")
                return

            partner_name = delivery.driver.get_full_name() or delivery.driver.username if delivery.driver else 'Delivery Partner'
            cust_name = getattr(order.customer_profile, 'name', None) or order.customer.get_full_name() or order.customer.username
            order_num = f"ORD-{order.id:04d}"

            # Delivered quantities summary
            delivered_items_list = list(delivery.delivered_items.select_related('product'))
            if delivered_items_list:
                qty_summary = ", ".join([f"{it.delivered_quantity}x {it.product.name}" for it in delivered_items_list[:3]])
            else:
                qty_summary = f"{order.items.count()} items"

            body_text = f"{order_num} ({cust_name}) delivered by {partner_name}. Quantities: {qty_summary}."

            cls.send_to_role(
                role='owner',
                title="Delivery Completed 🎉",
                body=body_text,
                data={
                    'type': 'delivery_completed',
                    'delivery_id': delivery_id,
                    'order_id': order.id,
                    'order_number': order_num,
                    'customer_name': cust_name,
                    'partner_name': partner_name,
                    'status': 'delivered',
                    'screen': 'Orders',
                    'dedup_key': dedup_key,
                }
            )

        transaction.on_commit(dispatch)
