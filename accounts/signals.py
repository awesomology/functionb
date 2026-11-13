from django.db.models.signals import post_save
from django.db import transaction
from django.dispatch import receiver
from django.core.mail import send_mail
from django.conf import settings
from .models import Transaction

@receiver(post_save, sender=Transaction)
def send_transaction_email(sender, instance, created, **kwargs):
    # 1. Only trigger for BRAND NEW transactions
    if not created:
        return
        
    # 2. Only send emails for successful incoming transfers
    if instance.status == Transaction.Status.SUCCESS and instance.transaction_type == Transaction.TransactionType.TRANSFER_IN:
        transaction.on_commit(lambda: _semd_mail_logic(instance.id))

        def _semd_mail_logic(transaction_id):
            try:
                trx = Transaction.objects.get(id=transaction_id)
                receiver_user = trx.account.user
                
                # Get the sender's name (if it exists)
                sender_name = "Someone"
                if trx.counterparty_account and trx.counterparty_account.user:
                    sender_name = f"{trx.counterparty_account.user.first_name} {trx.counterparty_account.user.last_name}"

                send_mail(
                    subject="Credit Alert",
                    message=f"Dear {receiver_user.first_name},\n\nYour account {trx.account.number} has been credited with ₦{trx.amount:,.2f} from {sender_name}.\n\nYour new balance is ₦{trx.account.balance:,.2f}.",
                    from_email=f"Gordon's Ledger <{settings.DEFAULT_FROM_EMAIL}>",
                    recipient_list=[receiver_user.email],
                    fail_silently=True, # Don't crash the transfer if the email fails
                )
            except:
                pass