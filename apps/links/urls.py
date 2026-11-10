from django.urls import path

from . import views

app_name = "links"
urlpatterns = [
    path("l/<str:token>/", views.link_page, name="page"),
]
