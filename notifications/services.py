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
            if template:
                url = f"{base_url}/{api_key}/SMS/+91{clean_mobile}/{urllib.parse.quote(message)}/{urllib.parse.quote(template)}"
            else:
                url = f"{base_url}/{api_key}/SMS/+91{clean_mobile}/{urllib.parse.quote(message)}"

            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'ChaiFlow-Backend/1.0', 'Accept': 'application/json'},
                method='GET',
            )
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
    def on_order_created(cls, order):
        """1. New customer order -> Notify Business Owners (FCM)"""
        transaction.on_commit(lambda: cls.send_to_role(
            role='owner',
            title="New Order Received! ☕",
            body=f"Order #{order.id} for ₹{order.total_amount} placed by {order.customer.username}.",
            data={
                'type': 'new_order',
                'order_id': order.id,
                'screen': 'Orders',
            }
        ))

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
            transaction.on_commit(lambda: cls.send_to_user(
                user=order.customer,
                title=title,
                body=body,
                data={
                    'type': 'order_status',
                    'order_id': order.id,
                    'status': order.status,
                    'screen': 'Orders',
                }
            ))

    @classmethod
    def on_delivery_assigned(cls, delivery):
        """3. Delivery assigned -> Notify Delivery Partner (FCM & SMS alert)"""
        if delivery.driver:
            transaction.on_commit(lambda: cls.send_to_user(
                user=delivery.driver,
                title="New Delivery Assigned 🚴",
                body=f"You have been assigned Order #{delivery.order.id} to {delivery.order.delivery_address}.",
                data={
                    'type': 'delivery_assigned',
                    'delivery_id': delivery.id,
                    'order_id': delivery.order.id,
                    'screen': 'OrderDetails',
                }
            ))
            # Also dispatch SMS alert to driver if registered mobile number exists
            driver_phone = getattr(getattr(delivery.driver, 'profile', None), 'phone', '')
            if driver_phone:
                transaction.on_commit(lambda: cls.send_sms(
                    phone=driver_phone,
                    message=f"ChaiFlow: You are assigned Order #{delivery.order.id}. Deliver to {delivery.order.delivery_address}.",
                    recipient=delivery.driver,
                    target_role='delivery',
                ))

    @classmethod
    def on_delivery_completed(cls, delivery):
        """4. Delivery marked completed -> Notify Owner"""
        transaction.on_commit(lambda: cls.send_to_role(
            role='owner',
            title="Order Delivered 🚴",
            body=f"Order #{delivery.order.id} has been successfully delivered by {delivery.driver.username if delivery.driver else 'partner'}.",
            data={
                'type': 'delivery_completed',
                'delivery_id': delivery.id,
                'order_id': delivery.order.id,
                'screen': 'Orders',
            }
        ))
