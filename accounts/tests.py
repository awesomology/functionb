from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from decimal import Decimal
from unittest.mock import patch
from django.utils import timezone
from django.test import override_settings
from .models import Account, Beneficiary, Transaction, OTPVerification

# Create your tests here.

User = get_user_model()

@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class TransferTests(APITestCase):
    
    def setUp(self):
        self.sender = User.objects.create(
            email="charles@example.com", first_name="Charles", last_name="Atula",
            phone="07011111111", is_active=True, kyc_tier=User.KYCTier.TIER_1 # type: ignore
        )
        self.sender.set_account_pin("123456") # type: ignore
        self.sender.set_transaction_pin("1234") # type: ignore
        self.sender.save()
        self.sender_account = Account.objects.create(
            user=self.sender, number="1111111111", balance=Decimal("50000.00")
        )

        self.receiver = User.objects.create(
            email="jane@example.com", first_name="Jane", last_name="Doe",
            phone="07022222222", is_active=True, kyc_tier=User.KYCTier.TIER_1 # type: ignore
        )
        self.receiver_account = Account.objects.create(
            user=self.receiver, number="2222222222", balance=Decimal("0.00")
        )

        self.client.force_authenticate(user=self.sender) # type: ignore
        self.transfer_url = reverse('transfer')
        self.transfer_otp_url = reverse('confirm-otp')

    # Add the patch here!
    @patch('accounts.signals.send_mail')
    def test_successful_transfer(self, mock_send_mail):
        """Test a successful transfer, transaction record and balance update."""
        data = {"receiver_account_number": "2222222222", "amount": "5000.00", "transaction_pin": "1234"}
        response = self.client.post(self.transfer_url, data)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.sender_account.refresh_from_db()
        self.receiver_account.refresh_from_db()
        self.assertEqual(self.sender_account.balance, Decimal("45000.00"))
        self.assertEqual(self.receiver_account.balance, Decimal("5000.00"))
        self.assertEqual(Transaction.objects.count(), 2)

    def test_insufficient_funds(self):
        """Test a user with insufficient balance."""
        # Lower balance to 10k
        self.sender_account.balance = Decimal("10000.00")
        self.sender_account.save()
        data = {"receiver_account_number": "2222222222", "amount": "20000.00", "transaction_pin": "1234"}
        response = self.client.post(self.transfer_url, data)
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Insufficient funds", response.json()['error'])

    def test_wrong_pin(self):
        """Test a user with the wrong pin being unauthorized."""
        data = {"receiver_account_number": "2222222222", "amount": "5000.00", "transaction_pin": "9999"}
        response = self.client.post(self.transfer_url, data)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_daily_limit_exceeded(self):
        """Test a user that has exceeded the daily limit."""
        data = {"receiver_account_number": "2222222222", "amount": "51000.00", "transaction_pin": "1234"}
        response = self.client.post(self.transfer_url, data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("daily limit is", response.json()['error'])

    # NEW TEST: High-value transfer triggers OTP
    @patch('accounts.views.send_mail') # Patch the send_mail in the view (OTP)
    def test_high_value_transfer_triggers_otp(self, mock_send_mail):
        """Test a user trying to transfer above the hardcoded \"High-Value\" -> 10,000"""
        data = {"receiver_account_number": "2222222222", "amount": "15000.00", "transaction_pin": "1234"}
        response = self.client.post(self.transfer_url, data)
        
        # Should return 202 Accepted
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(response.json()['requires_otp'])
        
        # Ensure OTP was created in DB
        self.assertEqual(OTPVerification.objects.filter(user=self.sender, purpose='transaction').count(), 1)
        
        # Ensure no money moved yet
        self.sender_account.refresh_from_db()
        self.assertEqual(self.sender_account.balance, Decimal("50000.00"))

    @patch('accounts.views.send_mail')
    def test_transfer_otp(self, mock_send_mail):
        """Test that after triggering the OTP, sending the correct OTP to /transfer/confirm-otp/ actually moves the money."""
        data = {
            "receiver_account_number": "2222222222",
            "amount": "15000",
            "transaction_pin": "1234"
        }

        response = self.client.post(self.transfer_url, data)
        self.assertTrue(response.json()['requires_otp'])
        self.assertEqual(OTPVerification.objects.filter(user=self.sender, purpose='transaction').count(), 1)
        code = OTPVerification.objects.get(user=self.sender).code

        check_data = {
            "otp_code": code
        }

        check_response = self.client.post(self.transfer_otp_url, check_data)

        self.assertIn("Transfer successful.", check_response.json()['message'])
        self.assertTrue(Transaction.objects.filter(account=self.sender_account, counterparty_account=self.receiver_account).exists())
        self.assertEqual(Transaction.objects.filter(account=self.sender_account, counterparty_account=self.receiver_account).count(), 1)

        self.sender_account.refresh_from_db()
        self.receiver_account.refresh_from_db()

        self.assertEqual(self.sender_account.balance, Decimal("35000.00"))
        self.assertEqual(self.receiver_account.balance, Decimal("15000.00"))

    def test_self_transfer(self):
        """Test that a user cannot send money to themselves."""
        data = {
            "receiver_account_number": "1111111111",
            "amount": "5000",
            "transaction_pin": "1234"
        }

        response = self.client.post(self.transfer_url, data)

        self.assertIn("Cannot transfer to yourself.", response.json()['error'])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_kyc_enforcement(self):
        """Test that a Tier 2 user can send ₦100,000 (which a Tier 1 user cannot)."""
        self.sender.kyc_tier = User.KYCTier.TIER_2 # type: ignore
        self.sender_account.balance = Decimal("500000.00")
        self.sender.save()
        self.sender_account.save()

        data = {
            "receiver_account_number": "2222222222",
            "amount": "100000",
            "transaction_pin": "1234"
        }

        response = self.client.post(self.transfer_url, data)

        self.assertTrue(response.json()['requires_otp'])

        code = OTPVerification.objects.get(user=self.sender).code
        
        check_data = {
            "otp_code": code
        }

        check_response = self.client.post(self.transfer_otp_url, check_data)

        self.assertEqual(self.sender.kyc_tier, User.KYCTier.TIER_2) # type: ignore
        self.assertEqual(check_response.status_code, status.HTTP_200_OK)
        self.assertIn("Transfer successful.", check_response.json()['message'])
        self.assertTrue(Transaction.objects.filter(account=self.sender_account, counterparty_account=self.receiver_account).exists())
        self.assertEqual(Transaction.objects.filter(account=self.sender_account, counterparty_account=self.receiver_account).count(), 1)

        self.sender_account.refresh_from_db()
        self.receiver_account.refresh_from_db()

        self.assertEqual(self.sender_account.balance, Decimal("400000.00"))
        self.assertEqual(self.receiver_account.balance, Decimal("100000.00"))

    def test_save_beneficiary_during_transfer(self):
        """Test that when you send a transfer with save_as_beneficiary=True, a Beneficiary record is created in the DB."""
        data = {
            "receiver_account_number": "2222222222",
            "amount": "5000",
            "transaction_pin": "1234",
            "save_as_beneficiary": True
        }

        response = self.client.post(self.transfer_url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Transaction.objects.filter(account=self.sender_account, counterparty_account=self.receiver_account).exists())
        self.assertTrue(Beneficiary.objects.filter(user=self.sender, account_number=self.receiver_account.number).exists())

    @patch('accounts.signals.send_mail') # Optional, just in case
    @patch('accounts.models.Account.save') # <--- The Magic: Mock the save method
    def test_failed_transaction(self, mock_account_save, mock_send_mail):
        """Test that a database failure rolls back the transfer and creates a FAILED record."""
        
        # Make the Account.save() method throw a database error
        mock_account_save.side_effect = Exception("Simulated Database Crash")

        data = {
            "receiver_account_number": "2222222222",
            "amount": "5000",
            "transaction_pin": "1234"
        }

        response = self.client.post(self.transfer_url, data)

        # 1. The API should return a 500 Internal Server Error
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # 2. The money should NOT have moved (atomic rollback worked)
        self.sender_account.refresh_from_db()
        self.receiver_account.refresh_from_db()
        self.assertEqual(self.sender_account.balance, Decimal("50000.00"))
        self.assertEqual(self.receiver_account.balance, Decimal("0.00"))

        # 3. A FAILED transaction record SHOULD exist
        failed_trx = Transaction.objects.filter(
            account=self.sender_account, 
            status=Transaction.Status.FAILED
        ).first()
        
        self.assertIsNotNone(failed_trx)
        self.assertEqual(failed_trx.amount, Decimal("5000.00")) # type: ignore
        
        # 4. No SUCCESS transactions should exist
        self.assertEqual(Transaction.objects.filter(status=Transaction.Status.SUCCESS).count(), 0)

@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AuthTests(APITestCase):

    
    def setUp(self):
        self.user = User.objects.create(
            email="test@example.com", first_name="Test", last_name="User",
            phone="07033333333", is_active=True
        )
        self.user.set_account_pin("123456") # type: ignore
        self.user.save()
        self.login_url = reverse('login')

    def test_login_success(self):
        data = {"identifier": "test@example.com", "account_pin": "123456"}
        response = self.client.post(self.login_url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.json())

    def test_login_wrong_pin(self):
        data = {"identifier": "test@example.com", "account_pin": "999999"}
        response = self.client.post(self.login_url, data)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_locked_account(self):
        # Simulate 3 failed attempts
        self.user.pin_attempts = 3 # type: ignore
        self.user.pin_locked_until = timezone.now() + timezone.timedelta(minutes=15) # type: ignore
        self.user.save()
        
        data = {"identifier": "test@example.com", "account_pin": "123456"}
        response = self.client.post(self.login_url, data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)