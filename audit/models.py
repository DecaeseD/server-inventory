from django.conf import settings
from django.db import models


class ImmutableError(Exception):
    pass


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableError("این سوابق قابل ویرایش نیستند.")

    def delete(self):
        raise ImmutableError("این سوابق قابل حذف نیستند.")

    def bulk_update(self, *a, **k):
        raise ImmutableError("این سوابق قابل ویرایش نیستند.")


class AppendOnlyModel(models.Model):
    """Base for tables that may only be INSERTed."""
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            raise ImmutableError("این سوابق قابل ویرایش نیستند.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableError("این سوابق قابل حذف نیستند.")


class AuditEvent(AppendOnlyModel):
    created_at = models.DateTimeField("زمان", auto_now_add=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT,
                             related_name="+", verbose_name="کاربر")
    action = models.CharField("عملیات", max_length=50, db_index=True)
    object_type = models.CharField("نوع شیء", max_length=50)
    object_id = models.CharField("شناسه شیء", max_length=50)
    document_id = models.BigIntegerField("سند مرتبط", null=True, blank=True, db_index=True)
    before = models.JSONField("قبل", null=True, blank=True)
    after = models.JSONField("بعد", null=True, blank=True)
    reason = models.TextField("علت", blank=True)
    ip = models.GenericIPAddressField("IP", null=True, blank=True)

    class Meta:
        verbose_name = "رویداد حسابرسی"
        verbose_name_plural = "رویدادهای حسابرسی"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["object_type", "object_id"])]


def record(*, user, action, obj_type, obj_id, document_id=None, before=None, after=None,
           reason="", ip=None):
    return AuditEvent.objects.create(
        user=user, action=action, object_type=obj_type, object_id=str(obj_id),
        document_id=document_id, before=before, after=after, reason=reason, ip=ip)
