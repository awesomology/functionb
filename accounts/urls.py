from django.urls import path
from . import views

urlpatterns = [
    path('signup/', views.SignupView.as_view(), name='signup'),
    path('verify-email/', views.VerifyEmailView.as_view(), name='verify-email'),
    path('resend-email/', views.ResendVerificationView.as_view(), name='resend-email'),
    path('login/', views.LoginView.as_view(), name='login'),
    path('logout/', views.LogoutView.as_view(), name='logout'),
    path('kyc/upload/', views.KYCUploadView.as_view(), name='kyc-upload'),
    path('kyc/file/<int:document_id>/', views.SecureKYCFileView.as_view(), name='secure-kyc-file'),
    path('kyc/status/', views.KYCStatusView.as_view(), name='kyc-status'),
    path('transfer/', views.TransactionView.as_view(), name='transfer'),
    path('transactions/', views.TransactionHistoryView.as_view(), name='transfer-history'),
    path('beneficiaries/', views.BeneficiaryListView.as_view(), name='beneficiaries'),
    path('beneficiaries/save-from-receipt/', views.SaveFromReceiptView.as_view(), name='save-from-receipt'),
    path('beneficiaries/delete/<int:pk>/', views.BeneficiaryDeleteView.as_view(), name='delete-beneficiary'),
    path('transfer/confirm-otp/', views.ConfirmTransactionOTPView.as_view(), name='confirm-otp'),
    path('apin-reset/request/', views.APinResetOTPRequest.as_view(), name='request-account-pin-otp'),
    path('apin-reset/request/confirm/', views.APinConfirmOTPRequest.as_view(), name='confirm-account-pin-otp'),
    path('tpin-reset/request/', views.TPinResetOTPRequest.as_view(), name='request-transaction-pin-otp'),
    path('tpin-reset/request/confirm/', views.TPinConfirmOTPRequest.as_view(), name='confirm-transaction-pin-otp'),
]