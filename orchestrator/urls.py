from django.urls import path
from .views import chatbot, verify_payment_dispatcher

urlpatterns = [
    path("api/chatbot/", chatbot, name="chatbot"),
    path("api/payments/verify/", verify_payment_dispatcher, name="verify_payment"),
]