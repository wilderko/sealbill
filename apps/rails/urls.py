from django.urls import path

from . import views

urlpatterns = [path("hooks/btcpay/<int:account_pk>/", views.btcpay_webhook, name="btcpay_webhook")]
