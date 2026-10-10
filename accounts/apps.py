from django.apps import AppConfig
from django.db.models.signals import post_migrate


def create_roles(sender, **kwargs):
    from django.contrib.auth.models import Group, Permission

    def perms(*codenames):
        return Permission.objects.filter(codename__in=codenames)

    admin_g, _ = Group.objects.get_or_create(name="مدیر کل")
    admin_g.permissions.set(Permission.objects.filter(
        content_type__app_label__in=["inventory", "catalog", "audit"]))
    keeper, _ = Group.objects.get_or_create(name="انباردار")
    keeper.permissions.set(perms("submit_document", "view_itemtype", "view_unit",
                                 "view_location", "view_category"))
    # Accountant: created EMPTY on purpose; the admin grants granular permissions
    # (view_prices, export_reports, view_stockdocument, ...). Nothing is implicit.
    Group.objects.get_or_create(name="حسابدار")


class AccountsConfig(AppConfig):
    name = "accounts"
    verbose_name = "کاربران و نقش‌ها"

    def ready(self):
        post_migrate.connect(create_roles, sender=self)
