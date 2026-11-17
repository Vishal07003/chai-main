from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from django.db import transaction
from accounts.models import UserProfile
from accounts.otp_service import OtpProviderService


class Command(BaseCommand):
    help = "Securely provisions the initial Chai Flow Owner/Administrator account for unified mobile OTP authentication."

    def add_arguments(self, parser):
        parser.add_argument(
            '--name',
            type=str,
            help='Owner full name (optional, will prompt if omitted)',
        )
        parser.add_argument(
            '--phone',
            type=str,
            help='10-digit Indian mobile number (optional, will prompt if omitted)',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("=== Chai Flow - Provision Initial Owner Account ==="))

        # 1. Check whether an Owner account already exists in the database
        existing_owner = UserProfile.objects.filter(role='owner').select_related('user').first()
        if existing_owner:
            owner_phone = existing_owner.phone
            masked_phone = f"{owner_phone[:2]}******{owner_phone[-2:]}" if len(owner_phone) == 10 else "******"
            raise CommandError(
                f"An Owner account already exists in the system (ending in {owner_phone[-4:]}). "
                "For security, multiple Owner account creation via this command is disallowed."
            )

        # 2. Prompt for Full Name
        full_name = options.get('name')
        if not full_name:
            full_name = input("Enter Owner Full Name: ").strip()

        if not full_name:
            raise CommandError("Owner full name is required.")

        # 3. Prompt for REQUIRED Mobile Number
        phone_input = options.get('phone')
        if not phone_input:
            phone_input = input("Enter REQUIRED Owner Mobile Number (10 digits): ").strip()
        else:
            phone_input = str(phone_input).strip()

        if not phone_input:
            raise CommandError("Owner mobile number is strictly REQUIRED and cannot be blank.")

        # 4. Normalize and validate mobile number to exactly 10 valid Indian digits
        clean_mobile = OtpProviderService.clean_phone(phone_input)
        if not clean_mobile or len(clean_mobile) != 10:
            raise CommandError(
                f"Invalid mobile number format: '{phone_input}'. "
                "Must be exactly 10 digits (e.g. 9876543210 or +919876543210)."
            )

        # 5. Prevent duplicate mobile numbers across all existing UserProfile records
        if UserProfile.objects.filter(phone=clean_mobile).exists():
            raise CommandError(
                f"Mobile number ending in {clean_mobile[-4:]} is already registered to an existing account. "
                "Duplicate mobile registrations are not permitted."
            )

        # 6. Atomic creation of User and UserProfile with role='owner'
        try:
            with transaction.atomic():
                username = f"owner_{clean_mobile}"
                
                # Check for existing username collision
                if User.objects.filter(username=username).exists():
                    user = User.objects.get(username=username)
                    user.first_name = full_name
                    user.is_staff = True
                    user.is_superuser = True
                    user.is_active = True
                    user.set_unusable_password()
                    user.save()
                else:
                    user = User.objects.create(
                        username=username,
                        first_name=full_name,
                        is_staff=True,
                        is_superuser=True,
                        is_active=True,
                    )
                    user.set_unusable_password()
                    user.save()

                UserProfile.objects.create(
                    user=user,
                    phone=clean_mobile,
                    role='owner',
                )

        except Exception as e:
            raise CommandError(f"Failed to create Owner account: {e}")

        masked_phone = f"+91 {clean_mobile[:2]}******{clean_mobile[-2:]}"
        self.stdout.write(self.style.SUCCESS("\n[SUCCESS] Initial Owner account provisioned successfully!"))
        self.stdout.write(f"Account Name  : {full_name}")
        self.stdout.write(f"Mobile Number : {masked_phone}")
        self.stdout.write(f"Assigned Role : owner (Superuser/Staff)")
        self.stdout.write(self.style.WARNING(
            "\nSecurity Note: No password or fixed OTP was assigned.\n"
            "Login will proceed securely through the standard mobile number + SMS OTP flow on your device."
        ))
