from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models

from audit.models import AppendOnlyModel, ImmutableError


class Bucket(models.TextChoices):
    FREE = "free", "آزاد"
    RESERVED = "reserved", "رزرو شده"
    INSTALLED = "installed", "نصب‌شده روی سرور"
    IN_REPAIR = "in_repair", "در تعمیر"
    AT_DATACENTER = "at_datacenter", "در دیتاسنتر"
    SCRAPPED = "scrapped", "اسقاط"


class UnitStatus(models.TextChoices):
    FREE = "free", "آزاد"
    RESERVED = "reserved", "رزرو شده"
    INSTALLED = "installed", "نصب‌شده روی سرور"
    IN_REPAIR = "in_repair", "در تعمیر"
    AT_DATACENTER = "at_datacenter", "در دیتاسنتر"
    SCRAPPED = "scrapped", "اسقاط"
    OUT = "out", "خارج از سیستم"


class Operation(models.TextChoices):
    PURCHASE = "purchase", "خرید و ورود"
    DC_RETURN = "dc_return", "برگشت از دیتاسنتر"
    REPAIR_RETURN = "repair_return", "برگشت از تعمیر"
    ADJUST_IN = "adjust_in", "افزایش (اصلاح موجودی)"
    ISSUE = "issue", "خروج / مصرف"
    ADJUST_OUT = "adjust_out", "کاهش (اصلاح موجودی)"
    INSTALL = "install", "نصب روی سرور"
    ENTER_INSTALLED = "enter_installed", "ورود قطعه‌ی نصب‌شده روی سرور"
    UNINSTALL = "uninstall", "جداسازی از سرور"
    EXIT_INSTALLED = "exit_installed", "خروج قطعه همراه سرور"
    SEND_DC = "send_dc", "ارسال به دیتاسنتر"
    SEND_REPAIR = "send_repair", "ارسال به تعمیر"
    SCRAP = "scrap", "اسقاط"
    TRANSFER = "transfer", "انتقال بین محل‌ها"
    REVERSAL = "reversal", "ابطال (معکوس)"


B = Bucket
# operation -> (from_bucket, to_bucket); None means "outside the system".
OPERATION_MOVES = {
    Operation.PURCHASE: (None, B.FREE),
    Operation.DC_RETURN: (B.AT_DATACENTER, B.FREE),
    Operation.REPAIR_RETURN: (B.IN_REPAIR, B.FREE),
    Operation.ADJUST_IN: (None, B.FREE),
    Operation.ISSUE: (B.FREE, None),
    Operation.ADJUST_OUT: (B.FREE, None),
    Operation.INSTALL: (B.FREE, B.INSTALLED),
    Operation.ENTER_INSTALLED: (None, B.INSTALLED),
    Operation.UNINSTALL: (B.INSTALLED, B.FREE),
    Operation.EXIT_INSTALLED: (B.INSTALLED, None),
    Operation.SEND_DC: (B.FREE, B.AT_DATACENTER),
    Operation.SEND_REPAIR: (B.FREE, B.IN_REPAIR),
    Operation.SCRAP: (B.FREE, B.SCRAPPED),
    Operation.TRANSFER: (B.FREE, B.FREE),
}

# Operations that need a server, and operations where a price is meaningful.
SERVER_OPERATIONS = {Operation.INSTALL, Operation.UNINSTALL, Operation.ENTER_INSTALLED,
                     Operation.EXIT_INSTALLED}
PRICED_OPERATIONS = {Operation.PURCHASE, Operation.ENTER_INSTALLED}


class InventorySettings(models.Model):
    """Singleton (pk=1). Global policies chosen by the admin."""
    allow_negative_stock = models.BooleanField("اجازه موجودی منفی", default=False)
    allow_self_approval = models.BooleanField("اجازه تأیید درخواست توسط ثبت‌کننده", default=False)
    currency = models.CharField("واحد پول", max_length=20, default="تومان")

    class Meta:
        verbose_name = "تنظیمات انبار"
        verbose_name_plural = "تنظیمات انبار"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableError("تنظیمات قابل حذف نیست.")


class Location(models.Model):
    name = models.CharField("نام", max_length=100, unique=True)
    is_active = models.BooleanField("فعال", default=True)

    class Meta:
        verbose_name = "محل نگهداری"
        verbose_name_plural = "محل‌های نگهداری"

    def __str__(self):
        return self.name


class Unit(models.Model):
    """A physical, individually identified item (serial-tracked parts and every server).
    State changes happen only through inventory.services."""
    item_type = models.ForeignKey("catalog.ItemType", on_delete=models.PROTECT, related_name="units")
    serial_number = models.CharField("شماره سریال", max_length=100, null=True, blank=True)
    asset_code = models.CharField("کد شناسایی داخلی", max_length=32, null=True, blank=True,
                                  unique=True, editable=False)
    label = models.CharField("برچسب (مثلاً شماره سرور)", max_length=100, blank=True)
    status = models.CharField("وضعیت", max_length=20, choices=UnitStatus.choices,
                              default=UnitStatus.OUT, db_index=True)
    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="units")
    installed_in = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="components", verbose_name="نصب‌شده روی")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "نمونه فیزیکی"
        verbose_name_plural = "نمونه‌های فیزیکی"
        constraints = [models.UniqueConstraint(fields=["item_type", "serial_number"],
                                               name="uniq_unit_serial_per_item")]
        indexes = [models.Index(fields=["item_type", "status", "location"])]

    def __str__(self):
        return f"{self.item_type.name} [{self.serial_number or self.label or self.asset_code or self.pk}]"

    @property
    def ident(self):
        """Best human identifier: serial if any, else the internal asset code."""
        return self.serial_number or self.asset_code or str(self.pk)


class StockDocument(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "در انتظار تأیید"
        APPROVED = "approved", "تأییدشده"
        REJECTED = "rejected", "ردشده"
        VOIDED = "voided", "باطل‌شده"

    occurred_at = models.DateTimeField("تاریخ وقوع")
    created_at = models.DateTimeField("تاریخ ثبت", auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name="+", verbose_name="ثبت‌کننده")
    status = models.CharField("وضعیت", max_length=10, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    note = models.TextField("توضیحات", blank=True)
    idempotency_key = models.CharField(max_length=64, unique=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name="+",
                                   verbose_name="تأییدکننده/ردکننده")
    decided_at = models.DateTimeField("تاریخ تأیید/رد", null=True, blank=True)
    reject_reason = models.TextField("دلیل رد", blank=True)
    void_reason = models.TextField("دلیل ابطال", blank=True)
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="reversed_by", verbose_name="معکوس‌کننده سند")

    class Meta:
        verbose_name = "سند انبار"
        verbose_name_plural = "اسناد انبار"
        ordering = ["-id"]
        permissions = [
            ("submit_document", "ثبت درخواست انبار"),
            ("approve_document", "تأیید/رد درخواست انبار"),
            ("void_document", "ابطال سند تأییدشده"),
            ("view_prices", "مشاهده قیمت‌ها"),
            ("export_reports", "خروجی گرفتن از گزارش‌ها"),
        ]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"سند #{self.pk}"

    @property
    def is_reversal(self):
        return self.reverses_id is not None


class StockLine(models.Model):
    document = models.ForeignKey(StockDocument, on_delete=models.PROTECT, related_name="lines")
    item_type = models.ForeignKey("catalog.ItemType", on_delete=models.PROTECT, related_name="lines")
    operation = models.CharField(max_length=20, choices=Operation.choices)
    quantity = models.PositiveIntegerField("تعداد")
    unit_price = models.DecimalField("قیمت واحد", max_digits=16, decimal_places=2, null=True, blank=True)
    total_price = models.DecimalField("مبلغ کل", max_digits=18, decimal_places=2, null=True, blank=True)
    from_bucket = models.CharField(max_length=20, choices=Bucket.choices, null=True, blank=True)
    to_bucket = models.CharField(max_length=20, choices=Bucket.choices, null=True, blank=True)
    from_location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", verbose_name="مبدأ")
    to_location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name="مقصد")
    server = models.ForeignKey(Unit, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="+", verbose_name="سرور مرتبط")
    server_serial = models.CharField("سریال سرور (سرور جدیدِ همین سند)", max_length=100, blank=True)
    serials = models.JSONField("شماره سریال‌ها", default=list, blank=True)
    note = models.CharField("توضیح", max_length=255, blank=True)
    parent_line = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="children", verbose_name="قلم والد (خودکار)")

    class Meta:
        verbose_name = "قلم سند"
        verbose_name_plural = "اقلام سند"
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gt=0),
                                              name="line_quantity_positive")]

    @property
    def direction(self):
        if self.from_bucket is None:
            return "increase"
        if self.to_bucket is None:
            return "decrease"
        return "move"

    def save(self, *args, **kwargs):
        if self.pk or self.document.status != StockDocument.Status.PENDING:
            raise ImmutableError("اقلام سند ثبت‌شده قابل ویرایش نیستند.")
        if self.unit_price is not None:
            self.total_price = (Decimal(self.unit_price) * self.quantity).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableError("اقلام سند قابل حذف نیستند.")


class LedgerEntry(AppendOnlyModel):
    """Immutable ledger. Stock = SUM(delta) over these rows. Written only by services."""
    document = models.ForeignKey(StockDocument, on_delete=models.PROTECT, related_name="ledger_entries")
    line = models.ForeignKey(StockLine, on_delete=models.PROTECT, related_name="ledger_entries")
    item_type = models.ForeignKey("catalog.ItemType", on_delete=models.PROTECT,
                                  related_name="ledger_entries")
    bucket = models.CharField(max_length=20, choices=Bucket.choices)
    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="+")
    server = models.ForeignKey(Unit, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="+")
    unit = models.ForeignKey(Unit, null=True, blank=True, on_delete=models.PROTECT,
                             related_name="ledger_entries")
    delta = models.IntegerField()
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "ورودی دفتر"
        verbose_name_plural = "دفتر موجودی"
        indexes = [models.Index(fields=["item_type", "bucket", "location", "server"]),
                   models.Index(fields=["occurred_at"])]
