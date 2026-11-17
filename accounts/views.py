from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework.exceptions import AuthenticationFailed
from django.contrib.auth.models import User
from django.db import transaction
from .serializers import UserSerializer, RegisterSerializer, UserProfileUpdateSerializer
from .models import UserProfile
from .otp_service import OtpProviderService
from deliveries.models import DeliveryPartner
from deliveries.serializers import DeliveryPartnerSerializer


class RoleTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Legacy/Fallback username+password endpoint for Owner access only.
    Delivery Partners must authenticate via Mobile OTP.
    """
    def validate(self, attrs):
        data = super().validate(attrs)
        user = self.user
        profile = getattr(user, 'profile', None)
        user_role = profile.role if profile else ('owner' if user.is_staff or user.is_superuser else 'customer')

        target_role = self.context['request'].data.get('target_role')
        if target_role:
            target_role = target_role.lower().strip()
            if target_role in ['owner', 'admin']:
                if user_role != 'owner' and not (user.is_staff or user.is_superuser):
                    raise AuthenticationFailed("This account is not authorized for Owner access.")
            elif target_role in ['delivery', 'delivery_partner']:
                raise AuthenticationFailed("Delivery Partners must authenticate via Mobile OTP.")

        data['user'] = UserSerializer(user).data
        return data


class RoleTokenObtainPairView(TokenObtainPairView):
    """
    POST /api/v1/auth/token/
    Owner login endpoint with strict role validation.
    """
    serializer_class = RoleTokenObtainPairSerializer


# =====================================================================
# UNIFIED MOBILE OTP AUTHENTICATION WITH AUTOMATIC ROLE DETECTION
# =====================================================================

class UnifiedRequestOtpView(APIView):
    """
    POST /api/v1/auth/request-otp/
    Single common OTP request endpoint for the entire Chai Flow mobile application.
    Identifies the registered account, verifies active status, and dispatches OTP.
    
    Request: { "phone": "9876543210" } or { "mobile_number": "9876543210" }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        phone_input = request.data.get('phone') or request.data.get('mobile_number')
        if not phone_input:
            return Response({'error': 'Mobile number is required.'}, status=status.HTTP_400_BAD_REQUEST)

        clean_mobile = OtpProviderService.clean_phone(phone_input)
        if not clean_mobile or len(clean_mobile) != 10:
            return Response({'error': 'Please enter a valid 10-digit mobile number.'}, status=status.HTTP_400_BAD_REQUEST)

        # Look up registered profile
        profile = UserProfile.objects.filter(phone=clean_mobile).select_related('user').first()
        if not profile or not profile.user:
            return Response({
                'error': 'This mobile number is not registered. Please contact the business administrator.'
            }, status=status.HTTP_404_NOT_FOUND)

        user = profile.user
        if not user.is_active:
            return Response({
                'error': 'This account has been deactivated. Please contact the administrator.'
            }, status=status.HTTP_403_FORBIDDEN)

        # If Delivery Partner, confirm partner profile is active
        if profile.role == 'delivery':
            partner = DeliveryPartner.objects.filter(user=user).first()
            if partner and hasattr(partner, 'is_active') and not partner.is_active:
                return Response({
                    'error': 'This delivery partner account has been deactivated by the owner.'
                }, status=status.HTTP_403_FORBIDDEN)

        # Dispatch OTP via provider
        result = OtpProviderService.send_otp(clean_mobile)
        if result.get('success'):
            return Response({
                'success': True,
                'message': result.get('message'),
                'session_id': result.get('session_id'),
                'cooldown_seconds': result.get('cooldown_seconds', 60),
            }, status=status.HTTP_200_OK)
        else:
            return Response({
                'success': False,
                'error': result.get('message'),
                'cooldown_seconds': result.get('cooldown_seconds', 0),
            }, status=status.HTTP_429_TOO_MANY_REQUESTS if 'wait' in result.get('message', '').lower() else status.HTTP_400_BAD_REQUEST)


class UnifiedVerifyOtpView(APIView):
    """
    POST /api/v1/auth/verify-otp/
    Single common OTP verification endpoint for the entire Chai Flow mobile application.
    Verifies 6-digit OTP, automatically detects actual database role (Owner vs Delivery Partner),
    and issues JWT tokens without trusting any frontend role parameter.

    Request: { "phone": "9876543210", "otp": "123456", "session_id": "optional" }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        phone_input = request.data.get('phone') or request.data.get('mobile_number')
        otp_code = request.data.get('otp')
        session_id = request.data.get('session_id', '')

        if not phone_input or not otp_code:
            return Response({'error': 'Mobile number and 6-digit OTP code are required.'}, status=status.HTTP_400_BAD_REQUEST)

        clean_mobile = OtpProviderService.clean_phone(phone_input)
        clean_otp = str(otp_code).strip()

        # Securely identify registered account
        profile = UserProfile.objects.filter(phone=clean_mobile).select_related('user').first()
        if not profile or not profile.user:
            return Response({
                'error': 'Unregistered account. Verification cannot proceed.'
            }, status=status.HTTP_404_NOT_FOUND)

        user = profile.user
        if not user.is_active:
            return Response({
                'error': 'This account has been deactivated. Access denied.'
            }, status=status.HTTP_403_FORBIDDEN)

        # Validate Delivery Partner specific active status
        partner_obj = None
        if profile.role == 'delivery':
            partner_obj = DeliveryPartner.objects.filter(user=user).first()
            if partner_obj and hasattr(partner_obj, 'is_active') and not partner_obj.is_active:
                return Response({
                    'error': 'This delivery partner account has been deactivated by the owner.'
                }, status=status.HTTP_403_FORBIDDEN)

        # Verify OTP
        with transaction.atomic():
            verify_result = OtpProviderService.verify_otp(clean_mobile, session_id, clean_otp)
            if not verify_result.get('success'):
                return Response({'error': verify_result.get('message')}, status=status.HTTP_400_BAD_REQUEST)

            # Automatic database role resolution
            if user.is_staff or user.is_superuser or profile.role == 'owner':
                resolved_role = 'owner'
            elif profile.role == 'delivery':
                resolved_role = 'delivery'
            else:
                resolved_role = 'customer'

            refresh = RefreshToken.for_user(user)
            user_data = UserSerializer(user).data
            partner_data = DeliveryPartnerSerializer(partner_obj).data if partner_obj else None

        return Response({
            'success': True,
            'message': f'Authenticated successfully as {resolved_role.title()}.',
            'role': resolved_role,
            'user': user_data,
            'partner': partner_data,
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            }
        }, status=status.HTTP_200_OK)


# =====================================================================
# Dedicated Delivery Partner & Legacy Routes (Preserved for compatibility)
# =====================================================================

class DeliveryRequestOtpView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        return UnifiedRequestOtpView().post(request)

class DeliveryVerifyOtpView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        return UnifiedVerifyOtpView().post(request)

class SendOtpView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        phone_input = request.data.get('phone') or request.data.get('mobile_number')
        if not phone_input:
            return Response({'error': 'Phone number is required.'}, status=status.HTTP_400_BAD_REQUEST)
        clean_mobile = OtpProviderService.clean_phone(phone_input)
        if not clean_mobile or len(clean_mobile) != 10:
            return Response({'error': 'Please enter a valid 10-digit mobile number.'}, status=status.HTTP_400_BAD_REQUEST)

        user_exists = UserProfile.objects.filter(phone=clean_mobile).exists()
        result = OtpProviderService.send_otp(clean_mobile)
        if result.get('success'):
            return Response({
                'success': True,
                'message': result.get('message'),
                'session_id': result.get('session_id'),
                'cooldown_seconds': result.get('cooldown_seconds', 60),
                'user_exists': user_exists,
            }, status=status.HTTP_200_OK)
        else:
            return Response({
                'success': False,
                'error': result.get('message'),
                'cooldown_seconds': result.get('cooldown_seconds', 0),
            }, status=status.HTTP_429_TOO_MANY_REQUESTS if 'wait' in result.get('message', '').lower() else status.HTTP_400_BAD_REQUEST)

class ResendOtpView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        return SendOtpView().post(request)

class VerifyOtpView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        phone_input = request.data.get('phone') or request.data.get('mobile_number')
        otp_code = request.data.get('otp')
        session_id = request.data.get('session_id', '')

        if not phone_input or not otp_code:
            return Response({'error': 'Phone number and OTP code are required.'}, status=status.HTTP_400_BAD_REQUEST)

        clean_mobile = OtpProviderService.clean_phone(phone_input)
        clean_otp = str(otp_code).strip()
        verify_result = OtpProviderService.verify_otp(clean_mobile, session_id, clean_otp)
        if not verify_result.get('success'):
            return Response({'error': verify_result.get('message')}, status=status.HTTP_400_BAD_REQUEST)

        target_role = request.data.get('target_role')
        if target_role:
            target_role = target_role.lower().strip()

        with transaction.atomic():
            profile = UserProfile.objects.filter(phone=clean_mobile).select_related('user').first()
            if profile:
                user = profile.user
                if target_role in ['owner', 'admin'] and profile.role != 'owner' and not (user.is_staff or user.is_superuser):
                    return Response({'error': 'This account is not authorized for Owner access.'}, status=status.HTTP_403_FORBIDDEN)
                elif target_role in ['delivery', 'delivery_partner'] and profile.role != 'delivery':
                    return Response({'error': 'This account is not authorized for Delivery Partner access.'}, status=status.HTTP_403_FORBIDDEN)
            else:
                if target_role in ['owner', 'admin', 'delivery', 'delivery_partner']:
                    return Response({'error': f'No authorized {target_role} account found.'}, status=status.HTTP_403_FORBIDDEN)
                username = f"user_{clean_mobile}"
                counter = 1
                base_username = username
                while User.objects.filter(username=username).exists():
                    username = f"{base_username}_{counter}"
                    counter += 1
                user = User.objects.create_user(username=username)
                profile = UserProfile.objects.create(user=user, phone=clean_mobile, role='customer')

        refresh = RefreshToken.for_user(user)
        return Response({
            'success': True,
            'user': UserSerializer(user).data,
            'tokens': {'refresh': str(refresh), 'access': str(refresh.access_token)}
        }, status=status.HTTP_200_OK)


# =====================================================================
# User Registration and Profile
# =====================================================================

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = [AllowAny]
    serializer_class = RegisterSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        refresh = RefreshToken.for_user(user)
        user_data = UserSerializer(user).data

        return Response({
            'user': user_data,
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            }
        }, status=status.HTTP_201_CREATED)


class CurrentUserProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if not user.is_active:
            raise AuthenticationFailed("This user account has been deactivated.")

        if hasattr(user, 'profile') and user.profile.role == 'delivery':
            partner = DeliveryPartner.objects.filter(user=user).first()
            if partner and hasattr(partner, 'is_active') and not partner.is_active:
                raise AuthenticationFailed("This delivery partner account has been deactivated by the owner.")

        serializer = UserSerializer(user)
        return Response(serializer.data)

    def patch(self, request):
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        serializer = UserProfileUpdateSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        request.user.refresh_from_db()
        return Response(UserSerializer(request.user).data)
