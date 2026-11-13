# accounts/models.py


from django.db import models
from django.db.models import Sum
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.validators import RegexValidator
from django.core.exceptions import ValidationError
from decimal import Decimal
import secrets
from django.conf import settings
from django.urls import reverse
from datetime import timedelta
from django.utils import timezone

# Create your models here.

def validate_ng_phone(value):
    # Remove any spaces or dashes
    clean_value = value.replace(" ", "").replace("-", "")

    # If it starts with +234, replace it with 0
    if clean_value.startswith("+234"):
        clean_value = "0" + clean_value[4:]
    # If it starts with 234, replace it with 0
    elif clean_value.startswith("234"):
        clean_value = "0" + clean_value[3:]

    # Now ensure it's exactly 11 digits and starts with 0
    if not clean_value.startswith("0") or len(clean_value) != 11 or not clean_value.isdigit():
        raise ValidationError("Phone number must be 11 digits, starting with 0 (or +234).")

    return clean_value

####################################################
#                    USER
####################################################
# Add this custom manager
class CustomUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('The Email field must be set')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self.create_user(email, password, **extra_fields)

class User(AbstractUser):
    username = None                              # login by email instead
    email = models.EmailField(unique=True)

    class VerificationStep(models.TextChoices):
        UNVERIFIED = "unverified", "Unverified"
        EMAIL_VERIFIED = "email_verified", "Email Verified"
        KYC_SUBMITTED = "kyc_submitted", "KYC Submitted"
        FULLY_VERIFIED = "fully_verified", "Fully Verified"

    verification_step = models.CharField(
        max_length=20,
        choices=VerificationStep.choices,
        default=VerificationStep.UNVERIFIED,
    )
    verification_completed_at = models.DateTimeField(null=True, blank=True)

    class KYCTier(models.IntegerChoices):
        TIER_1 = 1, "Unverified (Tier 1)"
        TIER_2 = 2, "Basic KYC (Tier 2)"
        TIER_3 = 3, "Full KYC (Tier 3)"

    kyc_tier = models.IntegerField(
        choices=KYCTier.choices, 
        default=KYCTier.TIER_1
    )

    first_name = models.CharField(max_length=120)
    last_name = models.CharField(max_length=120)
    phone = models.CharField(
        max_length=15,
        unique=True,
        validators=[validate_ng_phone],
    )
    bio = models.TextField(blank=True)

    # Store PINs as hashes, never plaintext. This field holds the hash.
    account_pin_hash = models.CharField(max_length=128, blank=True, null=True)
    transaction_pin_hash = models.CharField(max_length=128, blank=True, null=True)
    pin_attempts = models.PositiveSmallIntegerField(default=0)
    pin_locked_until = models.DateTimeField(null=True, blank=True)

    objects = CustomUserManager() # type: ignore
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name', 'phone']

    def set_account_pin(self, raw_pin: str):
        from django.contrib.auth.hashers import make_password
        self.account_pin_hash = make_password(raw_pin)

    def check_account_pin(self, raw_pin: str) -> bool:
        from django.contrib.auth.hashers import check_password
        if not self.account_pin_hash:
            return False
        return check_password(raw_pin, self.account_pin_hash)

    def set_transaction_pin(self, raw_pin: str):
        from django.contrib.auth.hashers import make_password
        self.transaction_pin_hash = make_password(raw_pin)

    def check_transaction_pin(self, raw_pin: str) -> bool:
        from django.contrib.auth.hashers import check_password
        if not self.transaction_pin_hash:
            return False
        return check_password(raw_pin, self.transaction_pin_hash)

    def get_daily_transfer_limit(self):
        if self.kyc_tier == self.KYCTier.TIER_2:
            return Decimal('500000.00')
        elif self.kyc_tier == self.KYCTier.TIER_3:
            return Decimal('1000000.00')
        return Decimal('50000.00') # Tier 1 limit

    def amount_sent_today(self):
        # Calculate midnight today
        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Sum all successful outgoing transfers since midnight
        total = Transaction.objects.filter(
            account__user=self,
            transaction_type=Transaction.TransactionType.TRANSFER_OUT,
            status=Transaction.Status.SUCCESS,
            created_at__gte=today
        ).aggregate(Sum('amount'))['amount__sum']
        
        return total or Decimal('0.00')

    def save(self, *args, **kwargs):
        # Normalize the phone number before saving
        if self.phone:
            self.phone = validate_ng_phone(self.phone)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email

####################################################
#                   ACCOUNT
####################################################
class Account(models.Model):
    class AccountType(models.TextChoices):
        SAVINGS = "savings", "Savings Account"
        CURRENT = "current", "Current Account"
        FIXED_DEPOSIT = "fixed_deposit", "Fixed Deposit"

    
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="accounts")
    account_type = models.CharField(
        max_length=20,
        choices=AccountType.choices,
        default=AccountType.SAVINGS,
    )
    number = models.CharField(max_length=10, unique=True)
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.user.first_name} {self.user.last_name} - {self.user.email} -> {self.account_type.capitalize()}"

class Transaction(models.Model):
    class TransactionType(models.TextChoices):
        DEPOSIT = "deposit", "Deposit"
        WITHDRAWAL = "withdrawal", "Withdrawal"
        TRANSFER_IN = "transfer_in", "Transfer In"
        TRANSFER_OUT = "transfer_out", "Transfer Out"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    sequence_number = models.PositiveIntegerField(editable=False) 

    # Which account is this affecting?
    account = models.ForeignKey('Account', on_delete=models.PROTECT, related_name="transactions")
    
    # Optional: If it's a transfer, link to the other account
    counterparty_account = models.ForeignKey(
        'Account', on_delete=models.PROTECT, null=True, blank=True, related_name="incoming_transactions"
    )
    
    transaction_type = models.CharField(max_length=20, choices=TransactionType.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reference = models.CharField(max_length=100, unique=True) # UUID or Bank Ref
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    
    # Store the balance at the time of the transaction (helps with auditing)
    balance_before = models.DecimalField(max_digits=14, decimal_places=2)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # This guarantees no two transactions for the same account can have the same sequence number
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'sequence_number'], 
                name='unique_sequence_per_account'
            )
        ]

    # Override the save method to auto-generate the sequence number
    def save(self, *args, **kwargs):
        if not self.sequence_number:
            # Find the highest sequence number for this specific account
            last_transaction = Transaction.objects.filter(account=self.account).order_by('-sequence_number').first()
            
            if last_transaction:
                self.sequence_number = last_transaction.sequence_number + 1
            else:
                self.sequence_number = 1 # First transaction for this account
                
        super().save(*args, **kwargs)

    def __str__(self):
        if self.transaction_type == self.TransactionType.TRANSFER_IN or self.transaction_type == self.TransactionType.DEPOSIT:
            return f"{self.transaction_type.capitalize()} to {self.account.user.first_name} {self.account.user.last_name} - {self.amount} - {self.status.capitalize()}"
        else:
            return f"{self.transaction_type.capitalize()} from {self.account.user.first_name} {self.account.user.last_name} - {self.amount} - {self.status.capitalize()}"

class Beneficiary(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="beneficiaries")
    name = models.CharField(max_length=100)
    account_number = models.CharField(max_length=10)
    bank_name = models.CharField(max_length=100)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # (As discussed earlier) A user can't save the same account twice
        unique_together = ('user', 'account_number', 'bank_name')
        verbose_name_plural = 'Beneficiaries'

    def __str__(self):
        return f"{f"{self.user.first_name} {self.user.last_name}"} saved {self.name} {self.bank_name}"

####################################################
#          SECURITY AND AUTHENTICATION
####################################################
class KYCDocument(models.Model):
    class DocumentType(models.TextChoices):
        NATIONAL_ID = "national_id", "National ID"
        PASSPORT = "passport", "International Passport"
        UTILITY_BILL = "utility_bill", "Utility Bill"
        SELFIE = "selfie", "Live Selfie"

    class VerificationStatus(models.TextChoices):
        PENDING = "pending", "Pending Review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="kyc_documents")
    document_type = models.CharField(max_length=20, choices=DocumentType.choices)
    
    # Store the actual file (Django handles the file storage path)
    file = models.FileField(upload_to='kyc_documents/%Y/%m/%d/') 
    
    status = models.CharField(max_length=20, choices=VerificationStatus.choices, default=VerificationStatus.PENDING)
    rejection_reason = models.TextField(blank=True, null=True)
    
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'KYC Document'

    def get_secure_url(self):
        # Points to a view we are about to create
        return reverse('secure-kyc-file', kwargs={'document_id': self.id}) # type: ignore

class LoginAttempt(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="login_attempts")
    identifier = models.CharField(max_length=100, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_info = models.CharField(max_length=255, blank=True) # e.g., "iPhone 14 - Chrome"
    was_successful = models.BooleanField(default=False)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Login Attempt'

    def __str__(self) -> str:
        return f"{self.user} - Was successful? {self.was_successful}"

class EmailVerificationToken(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_token")
    token = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Email Verification Token'

    def save(self, *args, **kwargs):
        if not self.token:
            # 32 bytes of randomness is considered secure for brute-force resistance 
            self.token = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Token for {self.user.email}"

class OTPVerification(models.Model):
    class Purpose(models.TextChoices):
        TRANSACTION = "transaction", "Transaction Authorization"
        ACCOUNT_PIN_RESET = "account_pin_reset", "Account Password Reset"
        TRANSACTION_PIN_RESET = "transaction_pin_reset", "Transaction Pin Reset"
        NEW_DEVICE_LOGIN = "new_device_login", "New Device Login"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    purpose = models.CharField(max_length=25, choices=Purpose.choices)
    code = models.CharField(max_length=6) 
    
    # Store the transfer details temporarily (e.g., receiver, amount)
    payload = models.JSONField(default=dict, null=True, blank=True) 
    
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        verbose_name = 'OTP Verification'

    def get_otp_code(self):
        return self.code
    
    @classmethod
    def generate_otp_code(cls, user, purpose, payload):
        """
        Generates a secure 6-digit OTP, saves it to the DB, and returns the object.
        """
        # Prevent spamming (max 1 OTP per minute)
        recent_otp = cls.objects.filter(
            user=user, 
            purpose=purpose, 
            created_at__gt=timezone.now() - timedelta(minutes=1)
        ).first()
        
        if recent_otp:
            raise ValueError("Please wait 1 minute before requesting another OTP.")
            
        otp_code = str(secrets.randbelow(1000000)).zfill(6)
        
        return cls.objects.create(
            user=user,
            purpose=purpose,
            code=otp_code,
            expires_at=timezone.now() + timedelta(minutes=5),
            payload=payload
        )

    def __str__(self) -> str:
        return f"{self.user.first_name} {self.user.last_name} - {self.is_used} - {self.purpose.capitalize()}"