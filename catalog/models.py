from django.core.exceptions import ValidationError
from django.db import models


class Category(models.Model):
    name = models.CharField("نام", max_length=100, unique=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="children", verbose_name="دسته والد")

    class Meta:
        verbose_name = "دسته‌بندی"
        verbose_name_plural = "دسته‌بندی‌ها"

    def __str__(self):
        return self.name


class ItemType(models.Model):
    class Tracking(models.TextChoices):
        QUANTITY = "quantity", "تعدادی"
        SERIAL = "serial", "سریالی"
        BOTH = "both", "هر دو"

    name = models.CharField("نام کالا", max_length=200)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="item_types",
                                 verbose_name="دسته‌بندی")
    brand = models.CharField("برند", max_length=100, blank=True)
    model_name = models.CharField("مدل", max_length=100, blank=True)
    description = models.TextField("توضیحات", blank=True)
    unit_label = models.CharField("واحد شمارش", max_length=30, default="عدد")
    tracking_mode = models.CharField("شیوه رهگیری", max_length=10, choices=Tracking.choices,
                                     default=Tracking.QUANTITY)
    is_server = models.BooleanField("سرور کامل است", default=False,
                                    help_text="فقط نمونه‌های این نوع می‌توانند میزبان قطعه باشند.")
    sku = models.CharField("کد کالا (اختیاری)", max_length=64, blank=True)
    class CapacityUnit(models.TextChoices):
        MB = "MB", "MB"
        GB = "GB", "GB"
        TB = "TB", "TB"
        CORE = "core", "هسته"

    capacity = models.DecimalField("ظرفیت هر عدد", max_digits=12, decimal_places=2, null=True, blank=True,
                                   help_text="مثلاً ۳۲ برای رم ۳۲ گیگ؛ برای جمع‌زدن مقدار کل روی سرور استفاده می‌شود.")
    capacity_unit = models.CharField("واحد ظرفیت", max_length=8, choices=CapacityUnit.choices, blank=True)
    min_stock = models.PositiveIntegerField("حداقل موجودی آزاد", default=0)
    is_active = models.BooleanField("فعال", default=True)

    class Meta:
        verbose_name = "نوع کالا"
        verbose_name_plural = "انواع کالا"
        ordering = ["name"]
        indexes = [models.Index(fields=["name"]), models.Index(fields=["sku"])]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orig_tracking = self.tracking_mode
        self._orig_is_server = self.is_server

    def __str__(self):
        return self.name

    def clean(self):
        if self.is_server and self.tracking_mode == self.Tracking.QUANTITY:
            raise ValidationError("سرور باید سریالی (یا هر دو) رهگیری شود.")
        if bool(self.capacity) != bool(self.capacity_unit):
            raise ValidationError("ظرفیت و واحد ظرفیت باید هر دو پر شوند یا هر دو خالی باشند.")
        if self.capacity is not None and self.capacity <= 0:
            raise ValidationError("ظرفیت باید بزرگتر از صفر باشد.")

    def save(self, *args, **kwargs):
        # Identity-level changes are blocked once the item has any history.
        if self.pk and (self.tracking_mode != self._orig_tracking
                        or self.is_server != self._orig_is_server):
            if self.ledger_entries.exists() or self.lines.exists():
                raise ValidationError(
                    "شیوه رهگیری یا نوع سرور بودن کالایی که سابقه تراکنش دارد قابل تغییر نیست.")
        super().save(*args, **kwargs)
        self._orig_tracking = self.tracking_mode
        self._orig_is_server = self.is_server


class AttributeDefinition(models.Model):
    name = models.CharField("نام ویژگی", max_length=100, unique=True)

    class Meta:
        verbose_name = "تعریف ویژگی"
        verbose_name_plural = "تعریف ویژگی‌ها"

    def __str__(self):
        return self.name


class ItemAttributeValue(models.Model):
    item_type = models.ForeignKey(ItemType, on_delete=models.CASCADE, related_name="attributes")
    definition = models.ForeignKey(AttributeDefinition, on_delete=models.PROTECT)
    value = models.CharField("مقدار", max_length=200)

    class Meta:
        verbose_name = "مقدار ویژگی"
        verbose_name_plural = "ویژگی‌ها"
        constraints = [models.UniqueConstraint(fields=["item_type", "definition"],
                                               name="uniq_item_attribute")]
        indexes = [models.Index(fields=["definition", "value"])]

    def __str__(self):
        return f"{self.definition.name}: {self.value}"
