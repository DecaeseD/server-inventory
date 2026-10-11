from django.apps import AppConfig, apps
from django.db.models.signals import post_migrate


def create_roles(sender=None, reset=False, **kwargs):
    """Create the three default roles. Permissions are assigned only when a group is
    first created (or reset=True), so admin customisations survive later migrations."""
    from django.contrib.auth.models import Group, Permission

    def perms(*codenames):
        return Permission.objects.filter(codename__in=codenames)

    g, created = Group.objects.get_or_create(name="مدیر کل")
    if created or reset:
        g.permissions.set(Permission.objects.filter(
            content_type__app_label__in=["inventory", "catalog", "audit"]))
    g, created = Group.objects.get_or_create(name="انباردار")
    if created or reset:
        g.permissions.set(perms("submit_document", "view_itemtype", "view_unit",
                                "view_location", "view_category"))
    # Accountant starts EMPTY on purpose: the admin grants granular permissions.
    Group.objects.get_or_create(name="حسابدار")


def _on_post_migrate(sender, app_config=None, **kwargs):
    # post_migrate is sent only for apps that have a models module (this app has none),
    # and permissions of every app must exist first => run once, after the LAST such app.
    last = [a for a in apps.get_app_configs() if a.models_module][-1]
    if app_config is last:
        create_roles()


class AccountsConfig(AppConfig):
    name = "accounts"
    verbose_name = "کاربران و نقش‌ها"

    def ready(self):
        post_migrate.connect(_on_post_migrate, dispatch_uid="accounts.create_roles")
