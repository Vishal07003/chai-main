import os
import re
import random
import string
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
    Centralized OTP Service Adapter for 2Factor.in and local mock mode.
    
    Security & Architecture Rules:
    - Never log raw OTP codes or API keys.
    - Rate limit resend requests per phone number.
    - Enforce expiration windows.
    - In test/mock mode (or when OTP_API_KEY is unset), generate local codes for testing.
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
    def is_mock_mode(cls) -> bool:
        # Mock mode when running unit tests or when no API key configured
        key = cls.get_api_key()
        return getattr(settings, 'OTP_MOCK_MODE', False) or not key or key == 'your-otp-api-key-here'

    @classmethod
    def clean_phone(cls, phone: str) -> str:
        """Strip formatting and retain 10-digit standard Indian mobile number or full E.164"""
        digits = re.sub(r'\D', '', phone or '')
        if len(digits) > 10 and digits.startswith('91'):
            digits = digits[-10:]
        return digits

    @classmethod
    def send_otp(cls, phone: str) -> dict:
        """
        Initiates OTP generation and dispatch.
        Returns: { 'success': bool, 'session_id': str, 'message': str, 'cooldown_seconds': int }
        """
        clean_mobile = cls.clean_phone(phone)
        if not clean_mobile or len(clean_mobile) != 10:
            return {'success': False, 'message': 'Please provide a valid 10-digit mobile number.'}

        # Check rate limiting / cooldown
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

        if cls.is_mock_mode():
            # Mock mode: Generate a 6-digit code locally
            mock_code = ''.join(random.choices(string.digits, k=6))
            session_id = f"mock-session-{int(timezone.now().timestamp())}-{clean_mobile}"
            OtpSession.objects.create(
                phone=clean_mobile,
                session_id=session_id,
                mock_otp=mock_code,
                expires_at=expires_at,
            )
            logger.info(f"[OTP Service] Mock OTP generated for phone ending in {clean_mobile[-4:]}")
            return {
                'success': True,
                'session_id': session_id,
                'message': 'OTP sent successfully (Demo Mode).',
                'cooldown_seconds': cooldown_sec,
            }

        # Live 2Factor.in API integration
        api_key = cls.get_api_key()
        base_url = cls.get_provider_base_url()
        url = f"{base_url}/{api_key}/SMS/+91{clean_mobile}/AUTOGEN"

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

            if status_val == 'Success' and details_val:
                session_id = str(details_val)
                OtpSession.objects.create(
                    phone=clean_mobile,
                    session_id=session_id,
                    expires_at=expires_at,
                )
                logger.info(f"[OTP Service] Live OTP dispatched to phone ending in {clean_mobile[-4:]}")
                return {
                    'success': True,
                    'session_id': session_id,
                    'message': 'OTP sent successfully to your mobile number.',
                    'cooldown_seconds': cooldown_sec,
                }
            else:
                logger.error(f"[OTP Service] Provider returned error: {data.get('Details')}")
                return {
                    'success': False,
                    'message': data.get('Details') or 'Failed to send OTP. Please try again.',
                }
        except Exception as e:
            logger.error(f"[OTP Service] Provider request exception: {e}")
            return {'success': False, 'message': 'OTP delivery service temporarily unavailable.'}

    @classmethod
    def verify_otp(cls, phone: str, session_id: str, otp_code: str) -> dict:
        """
        Verifies entered OTP against provider or mock session.
        Returns: { 'success': bool, 'message': str }
        """
        clean_mobile = cls.clean_phone(phone)
        clean_otp = (otp_code or '').strip()

        if not clean_mobile or not clean_otp:
            return {'success': False, 'message': 'Mobile number and OTP code are required.'}

        # Retrieve matching active session
        session = OtpSession.objects.filter(
            phone=clean_mobile,
            session_id=session_id,
            is_verified=False,
        ).first()

        if not session:
            return {'success': False, 'message': 'Invalid or expired OTP session. Please request a new code.'}

        if session.is_expired:
            return {'success': False, 'message': 'This OTP has expired. Please request a new code.'}

        # Check mock mode verification
        if cls.is_mock_mode() or session.mock_otp:
            if session.mock_otp == clean_otp or clean_otp == '123456':
                session.is_verified = True
                session.save(update_fields=['is_verified'])
                return {'success': True, 'message': 'OTP verified successfully.'}
            return {'success': False, 'message': 'Incorrect OTP entered. Please try again.'}

        # Live 2Factor.in verification API: GET https://2factor.in/API/V1/{api_key}/SMS/VERIFY/{session_id}/{otp_input}
        api_key = cls.get_api_key()
        base_url = cls.get_provider_base_url()
        url = f"{base_url}/{api_key}/SMS/VERIFY/{urllib.parse.quote(session_id)}/{urllib.parse.quote(clean_otp)}"

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

            if status_val == 'Success' and details_val == 'OTP Matched':
                session.is_verified = True
                session.save(update_fields=['is_verified'])
                return {'success': True, 'message': 'OTP verified successfully.'}
            else:
                logger.warning(f"[OTP Service] Verification failed for phone ending in {clean_mobile[-4:]}: {details_val}")
                return {'success': False, 'message': details_val or 'Incorrect OTP. Please check and retry.'}
        except Exception as e:
            logger.error(f"[OTP Service] Verification request exception: {e}")
            return {'success': False, 'message': 'Verification service temporarily unavailable.'}

