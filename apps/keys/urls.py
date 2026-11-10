from django.urls import path

from . import views

app_name = "keys"
urlpatterns = [
    path("i/<str:token>/key/", views.upload, name="upload"),
    path("k/<str:token>/", views.confirm, name="confirm"),
    # The token comes first, so /k/<token>/ stays the only shape the log filter needs to redact.
    path("k/<str:token>/cancel/", views.cancel, name="cancel"),
]
