from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth.models import User
from django.db import transaction
from .serializers import UserSerializer, RegisterSerializer, UserProfileUpdateSerializer
from .models import UserProfile
from .otp_service import OtpProviderService

class SendOtpView(APIView):
    """
    POST /api/v1/accounts/otp/send/
    Dispatches OTP to mobile number via 2Factor provider API.
    Request: { "phone": "9876543210" }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        phone = request.data.get('phone')
        if not phone:
            return Response({'error': 'Phone number is required.'}, status=status.HTTP_400_BAD_REQUEST)

        clean_mobile = OtpProviderService.clean_phone(phone)
        if not clean_mobile or len(clean_mobile) != 10:
            return Response({'error': 'Please enter a valid 10-digit mobile number.'}, status=status.HTTP_400_BAD_REQUEST)

        # Check if user exists (informational flag for client UI)
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
    """
    POST /api/v1/accounts/otp/resend/
    Re-sends OTP to mobile number with cooldown check.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        phone = request.data.get('phone')
        if not phone:
            return Response({'error': 'Phone number is required.'}, status=status.HTTP_400_BAD_REQUEST)

        result = OtpProviderService.send_otp(phone)
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


class VerifyOtpView(APIView):
    """
    POST /api/v1/accounts/otp/verify/
    Verifies OTP through provider. On success:
    - If user exists: logs them in, returns JWT access/refresh tokens and profile.
    - If user does not exist: creates new customer account (always 'customer' role).
    - Preserves existing owner/delivery roles and permissions.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        phone = request.data.get('phone')
        session_id = request.data.get('session_id')
        otp_code = request.data.get('otp')

        if not phone or not session_id or not otp_code:
            return Response({
                'error': 'Phone number, session_id, and OTP code are required.'
            }, status=status.HTTP_400_BAD_REQUEST)

        clean_mobile = OtpProviderService.clean_phone(phone)
        verify_result = OtpProviderService.verify_otp(clean_mobile, session_id, otp_code)

        if not verify_result.get('success'):
            return Response({'error': verify_result.get('message')}, status=status.HTTP_400_BAD_REQUEST)

        # OTP verified: Fetch existing account or create new customer
        with transaction.atomic():
            profile = UserProfile.objects.filter(phone=clean_mobile).select_related('user').first()

            if profile:
                user = profile.user
            else:
                # Public registration via OTP always gets 'customer' role
                username = f"user_{clean_mobile}"
                counter = 1
                base_username = username
                while User.objects.filter(username=username).exists():
                    username = f"{base_username}_{counter}"
                    counter += 1

                user = User.objects.create_user(username=username)
                profile = UserProfile.objects.create(
                    user=user,
                    phone=clean_mobile,
                    role='customer',
                    address='',
                )

        # Issue standard JWT tokens
        refresh = RefreshToken.for_user(user)
        user_data = UserSerializer(user).data

        return Response({
            'success': True,
            'user': user_data,
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            }
        }, status=status.HTTP_200_OK)


class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = [AllowAny]
    serializer_class = RegisterSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        # Generate JWT tokens immediately on registration for convenience
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
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

    def patch(self, request):
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        serializer = UserProfileUpdateSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        request.user.refresh_from_db()
        return Response(UserSerializer(request.user).data)
