from rest_framework import serializers
from .models import Account, Transaction, KYCDocument, Beneficiary

class TransactionSerializer(serializers.ModelSerializer):
    # This pulls the account number from the related Account model
    account_number = serializers.CharField(source='account.number', read_only=True)
    account_fname = serializers.CharField(source='account.user.first_name', read_only=True)
    account_lname = serializers.CharField(source='account.user.last_name', read_only=True)

    counterparty_number = serializers.SerializerMethodField()
    counterparty_name = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = [
            'id', 
            'sequence_number',
            'account_fname',
            'account_lname',
            'account_number',
            'counterparty_number',
            'counterparty_name',
            'transaction_type', 
            'amount', 
            'reference', 
            'status', 
            'balance_before', 
            'balance_after', 
            'created_at'
        ]

    def get_counterparty_number(self, obj):
        # If there is a counterparty account linked, show its number
        if obj.counterparty_account:
            return obj.counterparty_account.number
        return "N/A" # For deposits or withdrawals

    def get_counterparty_name(self, obj):
        if obj.counterparty_account:
            user = obj.counterparty_account.user
            return f"{user.first_name} {user.last_name}"
        return "Bank / ATM"

class KYCDocumentSerializer(serializers.ModelSerializer):
    file = serializers.FileField(write_only=True, required=True)
    secure_file_url = serializers.SerializerMethodField()

    class Meta:
        model = KYCDocument
        fields = ['id', 'document_type', 'file', 'secure_file_url', 'status', 'rejection_reason', 'uploaded_at']
        read_only_fields = ['status', 'rejection_reason', 'uploaded_at']

    def validate_file(self, value):
        # Security: Ensure the file is an image and not a massive 100MB file
        if not value.name.lower().endswith(('.png', '.jpg', '.jpeg')):
            raise serializers.ValidationError("Only PNG and JPEG images are allowed.")
        if value.size > 5 * 1024 * 1024: # 5MB limit
            raise serializers.ValidationError("File size cannot exceed 5MB.")
        return value

    def get_secure_file_url(self, obj):
        # Returns the secure bouncer URL instead of the public media URL
        if not obj.pk or not obj.file:
            return None
        
        request = self.context.get('request')
        if not request:
            return obj.get_secure_url()
        
        secure_url = obj.get_secure_url()
        return request.build_absolute_uri(secure_url)

class BeneficiarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Beneficiary
        fields = ['id', 'name', 'account_number']
        read_only_fields = ['id']