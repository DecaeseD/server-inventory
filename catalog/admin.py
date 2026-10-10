from django.contrib import admin
from audit.models import record
from .models import AttributeDefinition, Category, ItemAttributeValue, ItemType


class AttrInline(admin.TabularInline):
    model = ItemAttributeValue
    extra = 1


def snapshot(it):
    return {f: getattr(it, f) for f in ("name", "brand", "model_name", "unit_label",
            "tracking_mode", "is_server", "min_stock", "is_active", "sku", "description")}


@admin.register(ItemType)
class ItemTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "brand", "model_name", "tracking_mode", "is_server", "is_active")
    list_filter = ("category", "tracking_mode", "is_server", "is_active")
    search_fields = ("name", "brand", "model_name", "sku")
    inlines = [AttrInline]

    def save_model(self, request, obj, form, change):
        before = None
        if change:
            old = ItemType.objects.get(pk=obj.pk)
            before = snapshot(old)
        super().save_model(request, obj, form, change)
        record(user=request.user, action="item_changed" if change else "item_created",
               obj_type="ItemType", obj_id=obj.pk, before=before, after=snapshot(obj),
               ip=request.META.get("REMOTE_ADDR"))

    def has_delete_permission(self, request, obj=None):
        # Items with history are deactivated, never deleted.
        if obj and (obj.ledger_entries.exists() or obj.lines.exists()):
            return False
        return super().has_delete_permission(request, obj)


admin.site.register(Category)
admin.site.register(AttributeDefinition)
