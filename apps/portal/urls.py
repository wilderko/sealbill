from django.urls import path

from . import views

app_name = "portal"
urlpatterns = [
    path("i/<str:token>/", views.invoice_page, name="invoice"),
    path("i/<str:token>/pdf", views.invoice_pdf, name="pdf"),
    path("p/<str:token>/", views.pay_page, name="pay"),
    path("p/<str:token>/declare", views.declare, name="declare"),
    path("p/<str:token>/quote/", views.quote, name="quote"),
    path("p/<str:token>/status", views.status, name="status"),
]
