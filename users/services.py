import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import EmailOTP, User

logger = logging.getLogger(__name__)


class CommunicationService:
    """Central place for user-facing system communication."""

    @staticmethod
    def send_email(*, to: str, subject: str, message: str) -> bool:
        try:
            sent = send_mail(
                subject,
                message,
                getattr(settings, "DEFAULT_FROM_EMAIL", None),
                [to],
                fail_silently=False,
            )
            logger.info("Email sent to %s subject=%s", to, subject)
            return sent > 0
        except Exception:
            logger.exception("Email delivery failed to %s subject=%s", to, subject)
            return False


class OTPService:
    @staticmethod
    def uses_fixed_stage_code() -> bool:
        return getattr(settings, "APP_ENV", "").lower() == "stage"

    @staticmethod
    def generate_code() -> str:
        if OTPService.uses_fixed_stage_code():
            return getattr(settings, "STAGE_FIXED_OTP_CODE", "00000")
        return f"{secrets.randbelow(1_000_000):06d}"

    @staticmethod
    @transaction.atomic
    def issue(user: User, *, purpose: str) -> tuple[EmailOTP, str]:
        ttl_minutes = getattr(settings, "EMAIL_OTP_TTL_MINUTES", 10)
        EmailOTP.objects.filter(
            user=user,
            purpose=purpose,
            consumed_at__isnull=True,
        ).update(consumed_at=timezone.now())

        code = OTPService.generate_code()
        otp = EmailOTP.objects.create(
            user=user,
            purpose=purpose,
            code_hash=make_password(code),
            expires_at=timezone.now() + timedelta(minutes=ttl_minutes),
            max_attempts=getattr(settings, "EMAIL_OTP_MAX_ATTEMPTS", 5),
        )
        logger.info("OTP issued user=%s purpose=%s otp_id=%s", user.pk, purpose, otp.pk)
        return otp, code

    @staticmethod
    def send(user: User, *, purpose: str) -> EmailOTP:
        otp, code = OTPService.issue(user, purpose=purpose)
        app_name = getattr(settings, "APP_NAME", "Guardian")
        if OTPService.uses_fixed_stage_code():
            logger.info(
                "Skipping OTP email in stage; fixed code active user=%s purpose=%s",
                user.pk,
                purpose,
            )
            return otp

        if purpose == EmailOTP.PURPOSE_LOGIN:
            subject = f"{app_name} login code"
            opening = "Use this code to complete your login:"
        elif purpose == EmailOTP.PURPOSE_PASSWORD_RESET:
            subject = f"{app_name} password reset code"
            opening = "Use this code to reset your password:"
        else:
            subject = f"{app_name} email verification code"
            opening = "Use this code to verify your email address:"

        delivered = CommunicationService.send_email(
            to=user.email,
            subject=subject,
            message=(
                f"{opening}\n\n{code}\n\n"
                f"This code expires in {getattr(settings, 'EMAIL_OTP_TTL_MINUTES', 10)} minutes. "
                "If you did not request it, you can ignore this email."
            ),
        )
        if not delivered:
            logger.warning("OTP email not delivered user=%s purpose=%s", user.pk, purpose)
        return otp

    @staticmethod
    @transaction.atomic
    def verify(user: User, *, purpose: str, code: str) -> tuple[bool, str]:
        otp = (
            EmailOTP.objects.select_for_update()
            .filter(user=user, purpose=purpose, consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if not otp:
            logger.info("OTP verify failed: no active OTP user=%s purpose=%s", user.pk, purpose)
            return False, "No active code. Please request a new one."
        if otp.is_expired:
            otp.consumed_at = timezone.now()
            otp.save(update_fields=["consumed_at"])
            logger.info("OTP verify failed: expired otp_id=%s user=%s", otp.pk, user.pk)
            return False, "This code has expired. Please request a new one."
        if otp.attempts >= otp.max_attempts:
            otp.consumed_at = timezone.now()
            otp.save(update_fields=["consumed_at"])
            logger.warning("OTP verify failed: attempts exceeded otp_id=%s user=%s", otp.pk, user.pk)
            return False, "Too many attempts. Please request a new code."

        otp.attempts += 1
        if not check_password(code, otp.code_hash):
            otp.save(update_fields=["attempts"])
            logger.info("OTP verify failed: wrong code otp_id=%s user=%s", otp.pk, user.pk)
            return False, "Invalid code. Please try again."

        otp.consumed_at = timezone.now()
        otp.save(update_fields=["attempts", "consumed_at"])
        logger.info("OTP verified otp_id=%s user=%s purpose=%s", otp.pk, user.pk, purpose)
        return True, "Code verified."
