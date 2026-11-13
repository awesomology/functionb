# accounts/admin.py

import random

from django import forms
from django.contrib import admin, messages
from django.shortcuts import render
from django.utils import timezone
from django.utils.html import format_html
from decimal import Decimal
from django.db import transaction as db_transaction
from django.contrib.auth.admin import UserAdmin
from .models import Beneficiary, User, Account, EmailVerificationToken, Transaction, LoginAttempt, KYCDocument, OTPVerification

# Register your models here.

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = (
        (None, {'fields': ('email', 'account_pin_hash', 'transaction_pin_hash')}),
        ('Personal info', {'fields': ('first_name', 'last_name', 'phone', 'bio')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
        ('Bank Info', {'fields': ('verification_step', 'kyc_tier', 'pin_attempts', 'pin_locked_until')}),
    )
    
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'first_name', 'last_name', 'phone', 'account_pin_hash', 'transaction_pin_hash'),
        }),
    )

    list_display = ('email', 'first_name', 'last_name', 'verification_step', 'kyc_tier', 'is_active')
    list_editable = ['verification_step', 'kyc_tier']
    search_fields = ('email', 'first_name', 'last_name', 'phone')
    ordering = ('email',)

@admin.register(KYCDocument)
class KYCDocumentAdmin(admin.ModelAdmin):
    list_display = ('user', 'document_type', 'status', 'uploaded_at', 'view_file')
    list_filter = ('status', 'document_type')

    # This adds a clickable "View File" link in the admin list
    def view_file(self, obj):
        if obj.file:
            # NOTE: In the admin, we can use the public URL because only admins can log into the admin
            return format_html('<a href="{}" target="_blank">View Document</a>', obj.get_secure_url())
        return "No File"

    # このコドをむずかしあります
    view_file.short_description = 'Document' # type: ignore

    def save_model(self, request, obj, form, change):
        # 1. Save the document first
        super().save_model(request, obj, form, change)

        # 2. Check if the user has BOTH documents approved
        user = obj.user
        
        # We look at the database to see if there are approved documents of each type
        has_approved_selfie = KYCDocument.objects.filter(
            user=user, 
            document_type=KYCDocument.DocumentType.SELFIE, 
            status=KYCDocument.VerificationStatus.APPROVED
        ).exists()

        has_approved_utility = KYCDocument.objects.filter(
            user=user, 
            document_type=KYCDocument.DocumentType.UTILITY_BILL, 
            status=KYCDocument.VerificationStatus.APPROVED
        ).exists()

        has_approved_national_id = KYCDocument.objects.filter(
            user=user, 
            document_type=KYCDocument.DocumentType.NATIONAL_ID, 
            status=KYCDocument.VerificationStatus.APPROVED
        ).exists()

        has_approved_passport = KYCDocument.objects.filter(
            user=user, 
            document_type=KYCDocument.DocumentType.PASSPORT, 
            status=KYCDocument.VerificationStatus.APPROVED
        ).exists()

        # 3. Only upgrade if BOTH are approved
        if has_approved_selfie and has_approved_utility:
            user.verification_step = User.VerificationStep.EMAIL_VERIFIED
            if user.kyc_tier < User.KYCTier.TIER_2:
                user.kyc_tier = User.KYCTier.TIER_2
            user.verification_completed_at = timezone.now()
            user.save()

        if has_approved_national_id and has_approved_passport:
            user.verification_step = User.VerificationStep.FULLY_VERIFIED
            if user.kyc_tier < User.KYCTier.TIER_3:
                user.kyc_tier = User.KYCTier.TIER_3
            user.verification_completed_at = timezone.now()
            user.save()
        
        # 4. If any document is rejected, we should downgrade them just in case
        elif obj.status == KYCDocument.VerificationStatus.REJECTED:
            if not (has_approved_selfie and has_approved_utility) and not (has_approved_national_id and has_approved_passport):
                user.verification_step = User.VerificationStep.EMAIL_VERIFIED
                user.kyc_tier = User.KYCTier.TIER_1
                user.save()

class CreditAccountForm(forms.Form):
    amount = forms.DecimalField(
        max_digits=14, 
        decimal_places=2, 
        min_value=0.01,
        help_text="Enter the amount to credit the selected accounts."
    )

@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ('number', 'user', 'balance', 'created_at')
    search_fields = ('number', 'user__email', 'user__first_name', 'user__last_name')
    actions = ['credit_account']

    @admin.action(description='Credit selected accounts (Add money)')
    def credit_account(self, request, queryset):
        if 'apply' in request.POST:
            form = CreditAccountForm(request.POST)
            if form.is_valid():
                amount = form.cleaned_data['amount']
                success_count = 0
                for account in queryset:
                    try:
                        with db_transaction.atomic():
                            # 1. Lock the row to prevent double-crediting
                            acc = Account.objects.select_for_update().get(id=account.id)
                            
                            # 2. Calculate balances
                            balance_before = acc.balance
                            acc.balance += amount
                            acc.save()

                            # 3. Create the ledger transaction
                            Transaction.objects.create(
                                account=acc,
                                transaction_type=Transaction.TransactionType.DEPOSIT,
                                amount=amount,
                                status=Transaction.Status.SUCCESS,
                                balance_before=balance_before,
                                balance_after=acc.balance,
                                reference=f"DEPOSIT-{acc.id}{random.randint(10000000000000, 99999999999999)}-{Transaction.objects.count() + 1}" # type: ignore
                            )
                            success_count += 1
                    except Exception as e:
                        self.message_user(request, f"Failed to credit {account.number}: {str(e)}", messages.ERROR)
                        continue

                self.message_user(request, f"Successfully credited ₦{amount} to {success_count} account(s).", messages.SUCCESS)
                return None
        else:
            form = CreditAccountForm()
        return render(request, "admin/credit_account_form.html", context={
            "title": "Credit Selected Account(s)",
            "form": form,
            "accounts": queryset,
            "action_checkbox": "selected",
        })

# admin.site.register(Account)
admin.site.register(Beneficiary)
admin.site.register(EmailVerificationToken)
admin.site.register(LoginAttempt)
admin.site.register(OTPVerification)
admin.site.register(Transaction)