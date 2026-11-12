import os
import logging
from django.conf import settings
from django.db import transaction
from .models import DeviceToken

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

        # Check for service account path in settings or env
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
    Reusable notification engine:
    - Formats payloads (title, body, type, entity IDs, navigation target)
    - Sends asynchronously/post-commit
    - Cleans up invalid/unregistered FCM tokens
    """

    @classmethod
    def send_to_user(cls, user, title: str, body: str, data: dict = None):
        """
        Send notification to all active devices registered to a specific user.
        """
        active_tokens = list(
            DeviceToken.objects.filter(user=user, is_active=True).values_list('token', flat=True)
        )
        if not active_tokens:
            return

        cls.send_multicast(active_tokens, title, body, data)

    @classmethod
    def send_to_role(cls, role: str, title: str, body: str, data: dict = None):
        """
        Send notification to all active devices of users with a specific role ('owner', 'delivery', etc.)
        """
        active_tokens = list(
            DeviceToken.objects.filter(
                user__profile__role=role,
                is_active=True
            ).values_list('token', flat=True)
        )
        if not active_tokens:
            return

        cls.send_multicast(active_tokens, title, body, data)

    @classmethod
    def send_multicast(cls, tokens: list, title: str, body: str, data: dict = None):
        if not tokens:
            return

        data_payload = {k: str(v) for k, v in (data or {}).items()}

        if not initialize_firebase():
            # In local dev / unconfigured environments: Log rather than fail
            logger.info(f"[FCM Mock Send] Title: {title} | Body: {body} | Data: {data_payload} | Recipients: {len(tokens)}")
            return

        try:
            from firebase_admin import messaging

            message = messaging.MulticastMessage(
                notification=messaging.Notification(title=title, body=body),
                data=data_payload,
                tokens=tokens,
            )
            response = messaging.send_multicast(message)

            # Handle failed / invalid tokens
            if response.failure_count > 0:
                invalid_tokens = []
                for idx, resp in enumerate(response.responses):
                    if not resp.success:
                        err_code = getattr(resp.exception, 'code', '')
                        # Only deactivate confirmed unregistered/invalid registration tokens
                        if err_code == 'registration-token-not-registered':
                            invalid_tokens.append(tokens[idx])

                if invalid_tokens:
                    DeviceToken.objects.filter(token__in=invalid_tokens).update(is_active=False)
                    logger.info(f"Deactivated {len(invalid_tokens)} invalid FCM device tokens.")

        except Exception as e:
            logger.error(f"Error dispatching FCM multicast message: {e}")

    # ==========================================
    # High-level Event Handlers
    # ==========================================

    @classmethod
    def on_order_created(cls, order):
        """1. New customer order -> Notify Owners"""
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
        """2-5. Order status progression -> Notify Customer"""
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
        """6. Delivery assigned -> Notify Delivery Partner"""
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

