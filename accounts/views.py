# accounts/views.py

import secrets
from datetime import timedelta
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone
from rest_framework import status, generics, serializers, mixins
from rest_framework.authentication import SessionAuthentication
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import RefreshToken, AccessToken
from django.contrib.auth import authenticate
from django.core.mail import send_mail
from django.core.exceptions import ValidationError
from django.conf import settings
from django.http import FileResponse, Http404
from django.db import transaction as db_transaction
from decimal import Decimal
import random
import uuid
from .models import User, Account, Beneficiary, EmailVerificationToken, LoginAttempt, KYCDocument, Transaction, OTPVerification, validate_ng_phone
from .serializers import BeneficiarySerializer, TransactionSerializer, KYCDocumentSerializer

# Create your views here.

##############################################
#             Helper Functions
##############################################
def process_pin_reset_request(identifier):
    """Helper to normalize identifier and find the user."""
    if identifier and (identifier.startswith('+') or identifier.startswith('234') or identifier.startswith('0')):
        try:
            identifier = validate_ng_phone(identifier)
        except:
            pass
    return User.objects.filter(Q(email=identifier) | Q(phone=identifier)).first()

def send_reset_otp(user, purpose, subject, message_prefix):
    """Helper to generate OTP and send email."""
    try:
        otp_obj = OTPVerification.generate_otp_code(user, purpose, payload={})
    except ValidationError as e:
        return

    send_mail(
        subject=subject,
        message=f"{message_prefix}: {otp_obj.code}. It expires in 5 minutes.",
        from_email=f"Gordon's Ledger <{settings.DEFAULT_FROM_EMAIL}>",
        recipient_list=[user.email],
        fail_silently=False,
    )

def validate_and_set_new_pin(user, otp_code, purpose, new_pin, pin_length, pin_type:str):
    """Helper to validate fields and set new account or transaction pin"""
    if len(str(new_pin)) != int(pin_length):
        raise ValidationError({"error": f"PIN must be {pin_length} digits."})
    
    otp_obj = OTPVerification.objects.filter(
        user=user,
        purpose=purpose,
        is_used=False,
        expires_at__gt=timezone.now() # Must not be expired
    ).order_by('-created_at').first()

    if not otp_obj:
        raise ValidationError({"error": "No valid pending OTP found, or it has expired."})

    if otp_obj.code != otp_code:
        raise ValidationError({"error": "Invalid OTP."})

    otp_obj.is_used = True
    otp_obj.save()

    if pin_type.lower() == "account":
        user.set_account_pin(new_pin)
    elif pin_type.lower() == "transaction":
        user.set_transaction_pin(new_pin)

    user.save()

class SignupView(APIView):
    def post(self, request):
        email = request.data.get('email')
        first_name = request.data.get('first_name')
        last_name = request.data.get('last_name')
        phone = request.data.get('phone')
        account_pin = request.data.get('account_pin')
        transaction_pin = request.data.get('transaction_pin')

        # 1. Validate required fields
        if not all([email, first_name, last_name, phone, account_pin, transaction_pin]):
            return Response({"error": "All fields are required."}, status=status.HTTP_400_BAD_REQUEST)
        
        if len(account_pin) != 6 or len(transaction_pin) != 4:
            return Response({"error": "Invalid PIN lengths. Account PIN must be 6 digits, Transaction PIN 4."}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(email=email).exists() or User.objects.filter(phone=phone).exists():
            return Response({"error": "User with this email or phone already exists."}, status=status.HTTP_400_BAD_REQUEST)

        # 2. Create the user securely
        try:
            # We use db_transaction.atomic so if anything fails (like account creation), the user isn't saved
            with db_transaction.atomic():
                user = User.objects.create(
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    phone=phone,
                    is_active=False,
                    verification_step=User.VerificationStep.UNVERIFIED,
                )

                # 1. Create the DB token
                token_obj = EmailVerificationToken.objects.create(user=user)

                # 2. Send the email
                verification_link = f"http://localhost:3000/verify-email?token={token_obj.token}"
                send_mail(
                    subject="Verify your Bank Account",
                    message=f"Welcome! Please verify your email by clicking here: {verification_link}",
                    from_email=f"Gordon's Ledger <{settings.DEFAULT_FROM_EMAIL}>",
                    recipient_list=[user.email],
                    fail_silently=False,
                )

                # Hash the PINs
                user.set_account_pin(account_pin)
                user.set_transaction_pin(transaction_pin)
                user.save()

                # Create their first Account (Generate a mock 10-digit account number)
                acc_number = str(random.randint(1000000000, 9999999999))
                Account.objects.create(user=user, number=acc_number)

            return Response({"message": "User created. Please check your email to verify."}, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class VerifyEmailView(APIView):
    def post(self, request):
        token_string = request.data.get('token')
        if not token_string:
            return Response({"error": "Token is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            token_obj = EmailVerificationToken.objects.get(token=token_string)
            user = token_obj.user

            if user.is_active:
                return Response({"message": "Account already verified."}, status=status.HTTP_200_OK)

            if timezone.now() > token_obj.created_at + timedelta(hours=24):
                return Response({"error": "Token expired"})

            # Activate
            user.is_active = True
            user.verification_step = User.VerificationStep.EMAIL_VERIFIED
            user.save()

            token_obj.delete()

            return Response({"message": "Email verified successfully."}, status=status.HTTP_200_OK)
        except EmailVerificationToken.DoesNotExist:
            return Response({"error": "Invalid or expired token."}, status=status.HTTP_400_BAD_REQUEST)

class ResendVerificationView(APIView):
    def post(self, request):
        email = request.data.get('email')
        try:
            user = User.objects.get(email=email)
            if user.is_active:
                return Response({"message": "Already verified."}, status=status.HTTP_400_BAD_REQUEST)

            # Delete old token and create a new one
            EmailVerificationToken.objects.filter(user=user).delete()
            new_token = EmailVerificationToken.objects.create(user=user)

            # Send email again...
            verification_link = f"http://localhost:3000/verify-email?token={new_token.token}"
            send_mail(
                subject="Verify your Bank Account",
                message=f"Welcome! Please verify your email by clicking here: {verification_link}",
                from_email=f"Gordon's Ledger <{settings.DEFAULT_FROM_EMAIL}>",
                recipient_list=[user.email],
                fail_silently=False,
            )

            return Response({"message": "Verification email resent."}, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            # Security: Don't reveal if email exists
            return Response({"message": "If this email exists, a link was sent."}, status=status.HTTP_200_OK)

class LoginView(APIView):
    def post(self, request):
        identifier = request.data.get('identifier')
        account_pin = request.data.get('account_pin')

        # 1. Log the attempt
        ip = request.META.get('REMOTE_ADDR')
        device = request.META.get('HTTP_USER_AGENT', '')

        if not identifier or not account_pin:
            return Response({"error": "Email/Phone and PIN required."}, status=status.HTTP_400_BAD_REQUEST)

        # If it looks like a phone number, normalize it
        if identifier and (identifier.startswith('+') or identifier.startswith('234') or identifier.startswith('0')):
            try:
                identifier = validate_ng_phone(identifier)
            except ValidationError:
                return Response({"error": "Invalid phone number format."}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(Q(email=identifier) | Q(phone=identifier)).first()
        if not user:
            LoginAttempt.objects.create(identifier=identifier, ip_address=ip, device_info=device, was_successful=False)
            return Response({"error": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

        # 2. Check if PIN is locked
        from django.utils import timezone
        if user.pin_locked_until and user.pin_locked_until > timezone.now():
            return Response({"error": "Account temporarily locked due to too many failed attempts."}, status=status.HTTP_403_FORBIDDEN)

        if not user.is_active:
            return Response({"error": "Please verify your email address first."}, status=status.HTTP_403_FORBIDDEN)

        # 3. Verify PIN manually
        if user.check_account_pin(account_pin):
            # Success! Reset failed attempts
            user.pin_attempts = 0
            user.pin_locked_until = None
            user.last_login = timezone.now()
            user.save()
            
            LoginAttempt.objects.create(user=user, ip_address=ip, device_info=device, was_successful=True)

            # Generate JWT Tokens for React/Flutter
            refresh = RefreshToken.for_user(user)
            return Response({
                "access": str(refresh.access_token),
                "refresh": str(refresh)
            }, status=status.HTTP_200_OK)
        else:
            # Failed PIN
            user.pin_attempts += 1
            if user.pin_attempts >= 3:
                # Lock for 15 minutes
                user.pin_locked_until = timezone.now() + timezone.timedelta(minutes=15)
            user.save()
            
            LoginAttempt.objects.create(user=user, ip_address=ip, device_info=device, was_successful=False)
            return Response({"error": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            refresh_token = request.data.get("refresh")
            if not refresh_token:
                return Response({"error": "Refresh token is required."}, status=status.HTTP_400_BAD_REQUEST)
                
            token = RefreshToken(refresh_token)
            token.blacklist()
            return Response({"message": "Successfully logged out."}, status=status.HTTP_205_RESET_CONTENT)
        except Exception as e:
            return Response({"error": "Invalid or expired token."}, status=status.HTTP_400_BAD_REQUEST)

class KYCUploadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        
        if user.verification_step == user.VerificationStep.UNVERIFIED:
            return Response({"error": "Please verify your email first."}, status=status.HTTP_403_FORBIDDEN)

        selfie_file = request.FILES.get('selfie_file')
        utility_file = request.FILES.get('utility_file')
        national_id_file = request.FILES.get('national_id_file')
        passport_file = request.FILES.get('passport_file')

        try:
            with db_transaction.atomic():
                # --- TIER 1 UPGRADE ---
                if user.kyc_tier == User.KYCTier.TIER_1:
                    if national_id_file or passport_file:
                        raise ValueError("You cannot upload National ID or Passport while in Tier 1. Please complete Tier 1 first.")

                    has_selfie = KYCDocument.objects.filter(user=user, document_type=KYCDocument.DocumentType.SELFIE, status=KYCDocument.VerificationStatus.APPROVED).exists()
                    has_utility = KYCDocument.objects.filter(user=user, document_type=KYCDocument.DocumentType.UTILITY_BILL, status=KYCDocument.VerificationStatus.APPROVED).exists()

                    if has_selfie and selfie_file:
                        raise ValueError("You have already uploaded a Selfie")

                    if has_utility and utility_file:
                        raise ValueError("You have already uploaded a Utility Bill")

                    if not has_selfie and not selfie_file:
                        raise ValueError("A selfie is required.")
                    if not has_utility and not utility_file:
                        raise ValueError("A utility bill is required.")

                    if selfie_file and not has_selfie:
                        selfie_serializer = KYCDocumentSerializer(data={'document_type': KYCDocument.DocumentType.SELFIE, 'file': selfie_file}, context={'request': request})
                        if not selfie_serializer.is_valid():
                            raise ValueError({"selfie": selfie_serializer.errors})
                        selfie_serializer.save(user=user)

                    if utility_file and not has_utility:
                        utility_serializer = KYCDocumentSerializer(data={'document_type': KYCDocument.DocumentType.UTILITY_BILL, 'file': utility_file}, context={'request': request})
                        if not utility_serializer.is_valid():
                            raise ValueError({"utility": utility_serializer.errors})
                        utility_serializer.save(user=user)

                # --- TIER 2 UPGRADE ---
                elif user.kyc_tier == User.KYCTier.TIER_2:
                    if selfie_file or utility_file:
                        raise ValueError("You have already completed Tier 1. Please upload National ID and Passport for Tier 2.")

                    has_national = KYCDocument.objects.filter(user=user, document_type=KYCDocument.DocumentType.NATIONAL_ID, status=KYCDocument.VerificationStatus.APPROVED).exists()
                    has_passport = KYCDocument.objects.filter(user=user, document_type=KYCDocument.DocumentType.PASSPORT, status=KYCDocument.VerificationStatus.APPROVED).exists()

                    if has_national and national_id_file:
                        raise ValueError("You have already uploaded a National ID")

                    if has_passport and passport_file:
                        raise ValueError("You have already uploaded a Passport")

                    if not has_national and not national_id_file:
                        raise ValueError("A national ID is required.")
                    if not has_passport and not passport_file:
                        raise ValueError("A passport is required.")

                    if national_id_file and not has_national:
                        national_serializer = KYCDocumentSerializer(data={'document_type': KYCDocument.DocumentType.NATIONAL_ID, 'file': national_id_file}, context={'request': request})
                        if not national_serializer.is_valid():
                            raise ValueError({"national_id": national_serializer.errors})
                        national_serializer.save(user=user)

                    if passport_file and not has_passport:
                        passport_serializer = KYCDocumentSerializer(data={'document_type': KYCDocument.DocumentType.PASSPORT, 'file': passport_file}, context={'request': request})
                        if not passport_serializer.is_valid():
                            raise ValueError({"passport": passport_serializer.errors})
                        passport_serializer.save(user=user)
                        User.objects.create()

                elif user.kyc_tier == User.KYCTier.TIER_3:
                    raise ValueError("You are already fully verified.")

                # If we get here, everything succeeded. Update the user.
                user.verification_step = User.VerificationStep.KYC_SUBMITTED
                user.save()

                return Response({"message": f"Documents submitted. Pending review."}, status=status.HTTP_201_CREATED)

        except ValueError as e:
            # This catches our manual raises AND the serializer errors
            return Response({"error": e.args[0] if e.args else "Invalid data."}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class SecureKYCFileView(APIView):
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, document_id):
        try:
            document = KYCDocument.objects.get(id=document_id)
        except KYCDocument.DoesNotExist:
            raise Http404

        # Security check: Only the owner OR an admin (staff) can view the file
        if request.user == document.user or request.user.is_staff:
            # Return the actual file
            return FileResponse(document.file.open(), as_attachment=False)
        else:
            return Response({"error": "You do not have permission to view this file."}, status=status.HTTP_403_FORBIDDEN)

class KYCStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        
        # Fetch all KYC documents belonging to this user
        documents = KYCDocument.objects.filter(user=user).order_by('-uploaded_at')
        
        # Serialize them (this will automatically generate the secure file URLs!)
        serializer = KYCDocumentSerializer(documents, many=True, context={'request': request})
        
        return Response({
            "current_verification_step": user.verification_step,
            "current_kyc_tier": user.kyc_tier,
            "documents": serializer.data
        }, status=status.HTTP_200_OK)

class TransactionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        sender = request.user
        receiver_account_number = request.data.get('receiver_account_number')
        amount = request.data.get('amount')
        transaction_pin = request.data.get('transaction_pin')
        save_as_beneficiary = request.data.get('save_as_beneficiary', False)

        if not all([receiver_account_number, amount, transaction_pin]):
            return Response({"error": "Missing fields."}, status=status.HTTP_400_BAD_REQUEST)

        if not sender.check_transaction_pin(transaction_pin):
            return Response({"error": "Invalid Transaction PIN."}, status=status.HTTP_401_UNAUTHORIZED)
        
        try:
            amount_dec = Decimal(amount)
            if amount_dec < 50:
                raise ValueError("Amount must be greater than 50.")
        except:
            return Response({"error": "Invalid amount."}, status=status.HTTP_400_BAD_REQUEST)

        daily_limit = sender.get_daily_transfer_limit()
        amount_sent_today = sender.amount_sent_today()
        
        if (amount_sent_today + amount_dec) > daily_limit:
            remaining_limit = daily_limit - amount_sent_today
            error_msg = (
                f"Transfer failed. You tried to send ₦{amount_dec:,.2f}, but your remaining daily limit is ₦{remaining_limit:,.2f}. "
            )
    
            # Add a helpful nudge if they are on the lowest tier
            if sender.kyc_tier == User.KYCTier.TIER_1 or sender.kyc_tier == User.KYCTier.TIER_2:
                error_msg += "Please complete your KYC to increase your limit."
            return Response({
                "error": error_msg
            }, status=status.HTTP_403_FORBIDDEN)

        try:
            receiver_account = Account.objects.get(number=receiver_account_number)
        except Account.DoesNotExist:
            return Response({"error": "Receiver account not found."}, status=status.HTTP_404_NOT_FOUND)

        if receiver_account.user == sender:
            return Response({"error": "Cannot transfer to yourself."}, status=status.HTTP_400_BAD_REQUEST)

        sender_account = sender.accounts.first()
        if sender_account.balance < amount_dec:
            return Response({"error": "Insufficient funds."}, status=status.HTTP_400_BAD_REQUEST)

        if amount_dec >= 10000:
            otp_obj = OTPVerification.generate_otp_code(sender, OTPVerification.Purpose.TRANSACTION, {
                "receiver_account_number": receiver_account_number,
                "amount": str(amount_dec),
                "save_as_beneficiary": save_as_beneficiary
            })
            otp_code = OTPVerification.get_otp_code(otp_obj)
            send_mail(
                subject="Transaction Authorization Required",
                message=f"Your OTP for this transfer is: {otp_code}. It expires in 5 minutes.",
                from_email=f"Gordon's Ledger <{settings.DEFAULT_FROM_EMAIL}>",
                recipient_list=[sender.email],
                fail_silently=False,
            )

            return Response({
                "message": "High-value transfer. OTP sent to your email.",
                "requires_otp": True
            }, status=status.HTTP_202_ACCEPTED)
        

        return self._execute_transfer(sender, receiver_account_number, amount, save_as_beneficiary)

    def _execute_transfer(self, sender, receiver_account_number, amount, save_as_beneficiary):
        try:
            receiver_account = Account.objects.get(number=receiver_account_number)
        except Account.DoesNotExist:
            return Response({"error": "Receiver account not found."}, status=status.HTTP_404_NOT_FOUND)

        if receiver_account.user == sender:
            return Response({"error": "Cannot transfer to yourself."}, status=status.HTTP_400_BAD_REQUEST)

        amount = Decimal(amount)
        sender_account = sender.accounts.first() # Assuming they only have one account for now

        if sender_account.balance < amount:
            return Response({"error": "Insufficient funds."}, status=status.HTTP_400_BAD_REQUEST)

        # Generate a unique reference once for this specific transfer
        transfer_reference = f"TRX-{uuid.uuid4().hex[:12].upper()}"

        try:
            with db_transaction.atomic():
                # Lock the rows to prevent race conditions (double spending)
                sender_acc = Account.objects.select_for_update().get(id=sender_account.id)
                receiver_acc = Account.objects.select_for_update().get(id=receiver_account.id) # type: ignore

                if sender_acc.balance < amount:
                    raise ValueError("Insufficient funds")

                sender_acc.balance -= amount
                receiver_acc.balance += amount

                sender_acc.save()
                receiver_acc.save()

                # Record the transactions (Debit and Credit)
                Transaction.objects.create(
                    account=sender_acc,
                    counterparty_account=receiver_acc,
                    transaction_type=Transaction.TransactionType.TRANSFER_OUT,
                    amount=amount,
                    status=Transaction.Status.SUCCESS,
                    balance_before=sender_acc.balance + amount,
                    balance_after=sender_acc.balance,
                    reference=f"{transfer_reference}-OUT"
                )
                
                Transaction.objects.create(
                    account=receiver_acc,
                    counterparty_account=sender_acc,
                    transaction_type=Transaction.TransactionType.TRANSFER_IN,
                    amount=amount,
                    status=Transaction.Status.SUCCESS,
                    balance_before=receiver_acc.balance - amount,
                    balance_after=receiver_acc.balance,
                    reference=f"{transfer_reference}-IN"
                )

                if save_as_beneficiary:
                    existing_beneficiary = Beneficiary.objects.filter(
                        user=sender, 
                        account_number=receiver_account.number
                    ).first()
                    
                    if existing_beneficiary:
                        if existing_beneficiary.is_deleted:
                            existing_beneficiary.is_deleted = False
                            existing_beneficiary.save()
                    else:
                        Beneficiary.objects.create(
                            user=sender,
                            account_number=receiver_account.number,
                            name=f"{receiver_account.user.first_name} {receiver_account.user.last_name}"
                        )

            return Response({"message": "Transfer successful."}, status=status.HTTP_200_OK)
        except Exception as e:
            Transaction.objects.create(
                account=sender_account,
                counterparty_account=receiver_account,
                transaction_type=Transaction.TransactionType.TRANSFER_OUT,
                amount=amount,
                status=Transaction.Status.FAILED,
                balance_before=sender_account.balance,
                balance_after=sender_account.balance,
                reference=f"{transfer_reference}-FAILED"
            )
            import traceback
            traceback.print_exc() 
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class TransactionHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request): # Changed to GET
        user = request.user
        
        # Query all transactions where the account belongs to this user
        # We use select_related to optimize the database query (prevents N+1 problem)
        transactions = Transaction.objects.filter(
            account__user=user
        ).select_related('account').order_by('-created_at') 

        # Pass the QuerySet to the serializer
        serializer = TransactionSerializer(transactions, many=True)

        return Response({
            "message": "Fetch successful",
            "transactions": serializer.data, # Pass the serialized JSON data
        }, status=status.HTTP_200_OK)

class BeneficiaryListView(generics.ListAPIView):
    serializer_class = BeneficiarySerializer
    permission_classes = [IsAuthenticated]

    # GET: Show the logged-in user's beneficiaries that are not deleted
    def get_queryset(self): # type: ignore
        return Beneficiary.objects.filter(user=self.request.user, is_deleted=False)

class SaveFromReceiptView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        account_number = request.data.get('account_number')
        
        if not account_number:
            return Response({"error": "Account number is required."}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user

        target_account = Account.objects.filter(number=account_number).first()
        if not target_account:
             return Response({"error": "Account not found."}, status=status.HTTP_404_NOT_FOUND)

        has_transacted = Transaction.objects.filter(
            account__user=user,
            counterparty_account=target_account,
            status=Transaction.Status.SUCCESS
        ).exists()

        if not has_transacted:
            # If they haven't transacted, they are trying to hack the system!
            return Response(
                {"error": "You can only save accounts you have previously transacted with."}, 
                status=status.HTTP_403_FORBIDDEN
            )

        beneficiary, created = Beneficiary.objects.get_or_create(
            user=user,
            account_number=account_number,
            defaults={
                'name': f"{target_account.user.first_name} {target_account.user.last_name}"
            }
        )

        if not created and beneficiary.is_deleted:
            beneficiary.is_deleted = False
            beneficiary.name = f"{target_account.user.first_name} {target_account.user.last_name}"
            beneficiary.save()

        return Response({"message": "Beneficiary saved successfully."}, status=status.HTTP_200_OK)

class BeneficiaryDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        # Find the beneficiary, but ONLY if it belongs to the logged-in user
        beneficiary = Beneficiary.objects.filter(id=pk, user=request.user, is_deleted=False).first()
        
        if not beneficiary:
            return Response({"error": "Beneficiary not found."}, status=status.HTTP_404_NOT_FOUND)
            
        # Soft delete
        beneficiary.is_deleted = True
        beneficiary.save()
        
        return Response({"message": "Beneficiary removed successfully."}, status=status.HTTP_204_NO_CONTENT)

class ConfirmTransactionOTPView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        sender = request.user
        otp_code = request.data.get('otp_code')

        if not otp_code:
            return Response({"error": "OTP code is required."}, status=status.HTTP_400_BAD_REQUEST)

        # 1. Find the latest valid OTP for this user
        otp_record = OTPVerification.objects.filter(
            user=sender,
            purpose=OTPVerification.Purpose.TRANSACTION,
            is_used=False,
            expires_at__gt=timezone.now() # Must not be expired
        ).order_by('-created_at').first()

        if not otp_record:
            return Response({"error": "No valid pending OTP found, or it has expired."}, status=status.HTTP_400_BAD_REQUEST)

        if otp_record.code != otp_code:
            return Response({"error": "Invalid OTP."}, status=status.HTTP_401_UNAUTHORIZED)

        # 2. OTP is valid. Mark it as used immediately.
        otp_record.is_used = True
        otp_record.save()

        # 3. Extract the transaction payload we saved earlier
        payload = otp_record.payload
        if not payload:
            return Response({"error": "Transaction details missing. Retry transaction."}, status=status.HTTP_400_BAD_REQUEST)
        
        # 4. Run the transaction
        # We use the helper method from TransactionView
        transaction_view_instance = TransactionView()
        return transaction_view_instance._execute_transfer(
            sender=sender,
            receiver_account_number=payload['receiver_account_number'],
            amount=Decimal(payload['amount']),
            save_as_beneficiary=payload['save_as_beneficiary']
        )

class APinResetOTPRequest(APIView):
    def post(self, request):
        identifier = request.data.get('identifier')

        if not identifier:
            return Response({"error": "Email/Phone required"})

        user = process_pin_reset_request(identifier)
        if user:
            send_reset_otp(user, OTPVerification.Purpose.ACCOUNT_PIN_RESET, "Account Pin Reset", "Your OTP for your account pin reset is")

        return Response({"message": "If this account exists, an OTP has been sent."}, status=status.HTTP_200_OK)

class APinConfirmOTPRequest(APIView):
    def post(self, request):
        identifier = request.data.get('identifier')
        otp_code = request.data.get('otp_code')
        new_pin = request.data.get('new_pin')

        if not all([identifier, otp_code, new_pin]):
            return Response({"error": "Missing fields."}, status=status.HTTP_400_BAD_REQUEST)

        user = process_pin_reset_request(identifier)
        if not user:
            return Response({"error": "Invalid OTP or identifier."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # If this fails, it jumps to the except block
            validate_and_set_new_pin(user, otp_code, OTPVerification.Purpose.ACCOUNT_PIN_RESET, new_pin, 6, "account")
            return Response({"message": "Account Pin changed successfully."}, status=status.HTTP_200_OK)
        except ValidationError as e:
            # This catches the error from the helper and returns a 400
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

class TPinResetOTPRequest(APIView):
    def post(self, request):
        identifier = request.data.get('identifier')

        if not identifier:
            return Response({"error": "Email/Phone required"})

        user = process_pin_reset_request(identifier)
        if user:
            send_reset_otp(user, OTPVerification.Purpose.TRANSACTION_PIN_RESET, "Transaction Pin Reset", "Your OTP for your transaction pin reset is")

        return Response({"message": "If this account exists, an OTP has been sent."}, status=status.HTTP_200_OK)

class TPinConfirmOTPRequest(APIView):
    def post(self, request):
        identifier = request.data.get('identifier')
        otp_code = request.data.get('otp_code')
        new_pin = request.data.get('new_pin')

        if not all([identifier, otp_code, new_pin]):
            return Response({"error": "Missing fields."}, status=status.HTTP_400_BAD_REQUEST)

        user = process_pin_reset_request(identifier)
        if not user:
            return Response({"error": "Invalid OTP or identifier."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # If this fails, it jumps to the except block
            validate_and_set_new_pin(user, otp_code, OTPVerification.Purpose.TRANSACTION_PIN_RESET, new_pin, 4, "transaction")
            return Response({"message": "Account Pin changed successfully."}, status=status.HTTP_200_OK)
        except ValidationError as e:
            # This catches the error from the helper and returns a 400
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


