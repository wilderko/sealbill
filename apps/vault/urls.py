from django.urls import path

from . import views

urlpatterns = [path("unlock/", views.unlock, name="unlock")]
