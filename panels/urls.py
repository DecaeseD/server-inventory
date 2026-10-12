from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "panels"

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("items/", views.item_list, name="items"),
    path("items/<int:pk>/", views.item_detail, name="item"),
    path("units/<int:pk>/", views.unit_detail, name="unit"),
    path("items/<int:pk>/units/action/", views.unit_action, name="unit_action"),
    path("items/<int:pk>/edit/", views.item_edit, name="item_edit"),
    path("units/<int:pk>/edit/", views.unit_edit, name="unit_edit"),
    path("units/<int:pk>/hardware/", views.server_action, name="server_action"),
    path("changes/<int:pk>/approve/", views.change_approve, name="change_approve"),
    path("changes/<int:pk>/reject/", views.change_reject, name="change_reject"),
    path("requests/new/", views.new_request, name="new_request"),
    path("requests/", views.my_requests, name="my_requests"),
    path("documents/<int:pk>/", views.document_detail, name="document"),
    path("documents/<int:pk>/approve/", views.document_approve, name="approve"),
    path("documents/<int:pk>/reject/", views.document_reject, name="reject"),
    path("documents/<int:pk>/void/", views.document_void, name="void"),
    path("approvals/", views.approvals, name="approvals"),
]
