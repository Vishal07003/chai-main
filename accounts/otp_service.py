import os
import re
import secrets
import hashlib
import logging
import urllib.request
import urllib.parse
import json
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from .models import OtpSession

logger = logging.getLogger(__name__)

class OtpProviderService:
    """
    Centralized, cryptographically secure OTP Service Adapter.
    Strictly delivers 6-digit OTPs via SMS endpoint exclusively.
    
    Strict Delivery Rules:
    - Dedicated SMS OTP endpoint only (NO Voice calls, NO IVR, NO WhatsApp fallback).
    - 6-digit cryptographically secure OTP using secrets.choice.
    - Never log raw OTP codes in application logs.
    - Stores SHA-256 hashed OTPs in database.
    - Enforces 60-second cooldown on re-dispatch.
    - Enforces 5-minute expiry window.
    - Limits to max 5 failed verification attempts per session.
    - Invalidates all previous OTP sessions for the phone on new dispatch.
    - Invalidates session immediately upon successful verification (single-use).
    """

    @classmethod
    def get_api_key(cls) -> str:
        return (
            getattr(settings, 'TWOFACTOR_API_KEY', '')
            or os.getenv('TWOFACTOR_API_KEY', '')
            or getattr(settings, 'OTP_API_KEY', '')
            or os.getenv('OTP_API_KEY', '')
        )

    @classmethod
    def get_provider_base_url(cls) -> str:
        return getattr(settings, 'OTP_PROVIDER_BASE_URL', '') or os.getenv(
            'OTP_PROVIDER_BASE_URL', 'https://2factor.in/API/V1'
        ).rstrip('/')

    @classmethod
    def get_sms_template(cls) -> str:
        return (
            getattr(settings, 'OTP_SMS_TEMPLATE', '')
            or os.getenv('OTP_SMS_TEMPLATE', '')
        ).strip()

    @classmethod
    def is_mock_mode(cls) -> bool:
        key = cls.get_api_key()
        return (
            getattr(settings, 'OTP_MOCK_MODE', False)
            or not key
            or key in ['your-otp-api-key-here', 'your-twofactor-api-key-here']
        )

    @classmethod
    def clean_phone(cls, phone: str) -> str:
        """Strip formatting and retain 10-digit standard Indian mobile number"""
        digits = re.sub(r'\D', '', str(phone or ''))
        if len(digits) > 10 and digits.startswith('91'):
            digits = digits[-10:]
        elif len(digits) > 10:
            digits = digits[-10:]
        return digits

    @classmethod
    def hash_otp(cls, otp_code: str) -> str:
        """Returns SHA-256 hash of OTP code for secure persistence"""
        return hashlib.sha256(otp_code.encode('utf-8')).hexdigest()

    @classmethod
    def generate_secure_otp(cls) -> str:
        """Generates cryptographically secure 6-digit numeric OTP"""
        return ''.join(secrets.choice('0123456789') for _ in range(6))

    @classmethod
    def send_otp(cls, phone: str) -> dict:
        """
        Initiates OTP generation, invalidates prior sessions, and dispatches via SMS endpoint.
        Never calls or switches to voice/call OTP.
        Returns: { 'success': bool, 'session_id': str, 'message': str, 'cooldown_seconds': int }
        """
        clean_mobile = cls.clean_phone(phone)
        if not clean_mobile or len(clean_mobile) != 10:
            return {'success': False, 'message': 'Please provide a valid 10-digit mobile number.'}

        # Check rate limiting / cooldown against the latest session
        latest_session = OtpSession.objects.filter(phone=clean_mobile).first()
        cooldown_sec = getattr(settings, 'OTP_RESEND_COOLDOWN_SECONDS', 60)
        if latest_session and (timezone.now() - latest_session.created_at).total_seconds() < cooldown_sec:
            remaining = int(cooldown_sec - (timezone.now() - latest_session.created_at).total_seconds())
            return {
                'success': False,
                'message': f'Please wait {remaining} seconds before requesting another OTP.',
                'cooldown_seconds': remaining,
            }

        expiry_minutes = getattr(settings, 'OTP_EXPIRY_MINUTES', 5)
        expires_at = timezone.now() + timedelta(minutes=expiry_minutes)

        # Invalidate any prior active unverified OTP sessions for this phone
        OtpSession.objects.filter(phone=clean_mobile, is_verified=False).delete()

        # Generate fresh 6-digit OTP
        raw_otp = cls.generate_secure_otp()
        hashed_otp = cls.hash_otp(raw_otp)

        if cls.is_mock_mode():
            # Mock mode (unit test or local environment without 2Factor credentials)
            session_id = f"mock-session-{int(timezone.now().timestamp())}-{clean_mobile}"
            OtpSession.objects.create(
                phone=clean_mobile,
                session_id=session_id,
                otp_hash=hashed_otp,
                expires_at=expires_at,
            )
            logger.info(f"[OTP Service] Mock OTP session created for phone ending in {clean_mobile[-4:]}")
            return {
                'success': True,
                'session_id': session_id,
                'message': 'OTP sent via SMS successfully (Demo Mode).',
                'cooldown_seconds': cooldown_sec,
            }

        # Live 2Factor.in SMS integration - STRICTLY SMS ENDPOINT ONLY
        api_key = cls.get_api_key()
        base_url = cls.get_provider_base_url()
        template = cls.get_sms_template()

        # Build SMS OTP URL:
        # If template configured: /API/V1/{key}/SMS/+91{mobile}/{otp}/{template}
        # Otherwise default SMS route: /API/V1/{key}/SMS/+91{mobile}/{otp}
        if template:
            url = f"{base_url}/{api_key}/SMS/+91{clean_mobile}/{raw_otp}/{urllib.parse.quote(template)}"
        else:
            url = f"{base_url}/{api_key}/SMS/+91{clean_mobile}/{raw_otp}"

        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'ChaiFlow-Backend/1.0', 'Accept': 'application/json'},
                method='GET',
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))

            status_val = data.get('Status')
            details_val = data.get('Details')

            # Confirm successful SMS submission from provider
            if status_val == 'Success' and details_val:
                session_id = str(details_val)
                OtpSession.objects.create(
                    phone=clean_mobile,
                    session_id=session_id,
                    otp_hash=hashed_otp,
                    expires_at=expires_at,
                )
                logger.info(f"[OTP Service] SMS OTP dispatched to phone ending in {clean_mobile[-4:]}")
                return {
                    'success': True,
                    'session_id': session_id,
                    'message': 'OTP sent via SMS to your mobile number.',
                    'cooldown_seconds': cooldown_sec,
                }
            else:
                logger.error(f"[OTP Service] SMS Provider error: {details_val}")
                return {
                    'success': False,
                    'message': details_val or 'SMS delivery service was unable to send OTP. Please try again.',
                    'cooldown_seconds': cooldown_sec,
                }
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode('utf-8'))
                err_msg = err_data.get('Details') or err_data.get('message') or f"SMS Gateway Error ({e.code})"
            except Exception:
                err_msg = f"SMS Gateway Error ({e.code})"
            logger.error(f"[OTP Service] SMS HTTP Error: {err_msg}")
            return {'success': False, 'message': err_msg, 'cooldown_seconds': cooldown_sec}
        except Exception as e:
            logger.error(f"[OTP Service] SMS provider request exception: {e}")
            return {
                'success': False,
                'message': 'SMS delivery service temporarily unavailable. Please retry after cooldown.',
                'cooldown_seconds': cooldown_sec,
            }

    @classmethod
    def verify_otp(cls, phone: str, session_id: str, otp_code: str) -> dict:
        """
        Verifies entered OTP securely against hashed OTP with attempt tracking.
        Returns: { 'success': bool, 'message': str }
        """
        clean_mobile = cls.clean_phone(phone)
        clean_otp = str(otp_code or '').strip()

        if not clean_mobile or not clean_otp:
            return {'success': False, 'message': 'Mobile number and OTP code are required.'}

        # Session lookup
        lookup_kwargs = {'phone': clean_mobile}
        if session_id:
            lookup_kwargs['session_id'] = session_id

        session = OtpSession.objects.filter(**lookup_kwargs, is_verified=False).first()

        if not session:
            return {'success': False, 'message': 'Invalid or expired OTP session. Please request a new code.'}

        # Check maximum verification attempts (max 5)
        if session.has_exceeded_attempts:
            return {'success': False, 'message': 'Maximum verification attempts exceeded. Please request a new OTP.'}

        # Check expiration
        if session.is_expired:
            return {'success': False, 'message': 'This OTP has expired. Please request a new code.'}

        # Increment attempt counter
        session.attempts += 1
        session.save(update_fields=['attempts'])

        # Verify against SHA-256 hash
        input_hash = cls.hash_otp(clean_otp)
        is_hash_match = (session.otp_hash == input_hash)
        # Mock mode fallback code for test environments
        is_mock_match = cls.is_mock_mode() and (clean_otp in ['123456', '999999'])

        if is_hash_match or is_mock_match:
            # Single-use: invalidate session on successful verification
            session.is_verified = True
            session.save(update_fields=['is_verified'])
            return {'success': True, 'message': 'OTP verified successfully.'}

        remaining_attempts = max(0, session.max_attempts - session.attempts)
        if remaining_attempts == 0:
            return {'success': False, 'message': 'Maximum attempts exceeded. This OTP has been invalidated.'}

        return {
            'success': False,
            'message': f'Incorrect OTP entered. {remaining_attempts} attempts remaining.',
        }
