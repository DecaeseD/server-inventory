from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied

from . import services
from .models import (InventorySettings, LedgerEntry, Location, StockDocument, StockLine, Unit)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False


class LineInline(admin.TabularInline):
    model = StockLine
    extra = 0
    can_delete = False
    def has_add_permission(self, request, obj=None): return False
    def has_change_permission(self, request, obj=None): return False


@admin.register(StockDocument)
class StockDocumentAdmin(ReadOnlyAdmin):
    list_display = ("id", "status", "created_by", "occurred_at", "created_at", "decided_by")
    list_filter = ("status",)
    inlines = [LineInline]
    actions = ["approve_selected"]

    @admin.action(description="تأیید اسناد انتخاب‌شده")
    def approve_selected(self, request, queryset):
        for doc in queryset:
            try:
                services.approve_document(doc.pk, request.user, ip=request.META.get("REMOTE_ADDR"))
                self.message_user(request, f"سند {doc.pk} تأیید شد.")
            except (services.InventoryError, PermissionDenied) as e:
                self.message_user(request, f"سند {doc.pk}: {e}", level=messages.ERROR)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.has_perm("inventory.approve_document"):
            actions.pop("approve_selected", None)
        return actions


@admin.register(Unit)
class UnitAdmin(ReadOnlyAdmin):
    list_display = ("item_type", "serial_number", "label", "status", "location", "installed_in")
    list_filter = ("status", "item_type", "location")
    search_fields = ("serial_number", "label")


@admin.register(LedgerEntry)
class LedgerAdmin(ReadOnlyAdmin):
    list_display = ("id", "document", "item_type", "bucket", "location", "server", "unit", "delta")
    list_filter = ("bucket", "item_type")


@admin.register(InventorySettings)
class SettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request): return not InventorySettings.objects.exists()
    def has_delete_permission(self, request, obj=None): return False


admin.site.register(Location)
