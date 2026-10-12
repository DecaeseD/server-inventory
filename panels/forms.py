import re

from django import forms
from django.utils import timezone

from catalog.models import ItemType
from inventory.models import Location, Operation, Unit, UnitStatus


class DocumentForm(forms.Form):
    occurred_at = forms.DateTimeField(
        label="تاریخ و زمان وقوع", input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"))
    note = forms.CharField(label="توضیحات", required=False,
                           widget=forms.Textarea(attrs={"rows": 2}))
    idempotency_key = forms.CharField(widget=forms.HiddenInput)

    def clean_occurred_at(self):
        v = self.cleaned_data["occurred_at"]
        if v > timezone.now() + timezone.timedelta(minutes=5):
            raise forms.ValidationError("تاریخ وقوع نمی‌تواند در آینده باشد.")
        return v


class ItemChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        extra = " ".join(x for x in (obj.brand, obj.model_name) if x)
        return f"{obj.name} — {extra}" if extra else obj.name


class LineForm(forms.Form):
    field_order = ["item_type", "operation", "quantity", "from_location", "to_location",
                   "server_serial", "serials", "unit_price", "note"]
    item_type = ItemChoice(label="کالا", queryset=ItemType.objects.none())
    operation = forms.ChoiceField(
        label="عملیات", choices=[(v, l) for v, l in Operation.choices
                 if v not in (Operation.REVERSAL, Operation.EXIT_INSTALLED)])
    quantity = forms.IntegerField(label="تعداد", min_value=1)
    unit_price = forms.DecimalField(label="قیمت واحد", required=False, min_value=0,
                                    max_digits=16, decimal_places=2)
    from_location = forms.ModelChoiceField(label="محل مبدأ", required=False,
                                           queryset=Location.objects.none())
    to_location = forms.ModelChoiceField(label="محل مقصد", required=False,
                                         queryset=Location.objects.none())
    server_serial = forms.CharField(
        label="سریال سرور", required=False, max_length=100,
        widget=forms.TextInput(attrs={"list": "server-serials", "autocomplete": "off", "dir": "ltr",
                                      "placeholder": "سریال را بنویس یا از لیست انتخاب کن"}),
        help_text="سروری که این قطعه روی آن است / نصب می‌شود. اگر سرور در همین درخواست وارد می‌شود، همان سریال را بنویس.")
    serials = forms.CharField(label="سریال‌ها / کد شناسایی (هر سطر یکی)", required=False,
                              widget=forms.Textarea(attrs={"rows": 2, "dir": "ltr"}))
    note = forms.CharField(label="توضیح قلم", required=False, max_length=255)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item_type"].queryset = ItemType.objects.filter(is_active=True).order_by("name")
        locs = Location.objects.filter(is_active=True).order_by("name")
        self.fields["from_location"].queryset = locs
        self.fields["to_location"].queryset = locs

    def to_line_dict(self):
        c = self.cleaned_data
        serials = [s.strip() for s in re.split(r"[\n,،]+", c.get("serials") or "") if s.strip()]
        ss = (c.get("server_serial") or "").strip()
        server = None
        if ss:  # one active server with this serial => link it; else keep the text (new server of this request)
            found = list(Unit.objects.filter(item_type__is_server=True, serial_number=ss)
                         .exclude(status__in=[UnitStatus.OUT, UnitStatus.SCRAPPED])[:2])
            if len(found) == 1:
                server, ss = found[0], ""
        return dict(item_type=c["item_type"], operation=c["operation"], quantity=c["quantity"],
                    unit_price=c.get("unit_price"), from_location=c.get("from_location"),
                    to_location=c.get("to_location"), server=server, server_serial=ss,
                    serials=serials, note=c.get("note", ""))


class UnitEditForm(forms.Form):
    serial_number = forms.CharField(label="شماره سریال", required=False, max_length=100,
                                    widget=forms.TextInput(attrs={"dir": "ltr"}))
    label = forms.CharField(label="برچسب", required=False, max_length=100)
    note = forms.CharField(label="توضیح برای تأییدکننده", required=False, max_length=255)

    def values(self):
        c = self.cleaned_data
        return {"serial_number": c["serial_number"], "label": c["label"]}


class ItemEditForm(forms.Form):
    name = forms.CharField(label="نام کالا", max_length=200)
    category = forms.ModelChoiceField(label="دسته‌بندی", queryset=None)
    brand = forms.CharField(label="برند", required=False, max_length=100)
    model_name = forms.CharField(label="مدل", required=False, max_length=100)
    sku = forms.CharField(label="کد کالا", required=False, max_length=64)
    unit_label = forms.CharField(label="واحد شمارش", max_length=30)
    min_stock = forms.IntegerField(label="حداقل موجودی آزاد", min_value=0)
    capacity = forms.DecimalField(label="ظرفیت هر عدد", required=False, min_value=0, max_digits=12,
                                  decimal_places=2, help_text="مثلاً ۳۲ برای رم ۳۲ گیگ (برای جمع‌زدن روی سرور)")
    capacity_unit = forms.ChoiceField(label="واحد ظرفیت", required=False,
                                      choices=[("", "—")] + list(ItemType.CapacityUnit.choices))
    is_active = forms.BooleanField(label="فعال", required=False)
    description = forms.CharField(label="توضیحات", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    note = forms.CharField(label="توضیح برای تأییدکننده", required=False, max_length=255)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from catalog.models import Category
        self.fields["category"].queryset = Category.objects.order_by("name")

    def values(self):
        c = dict(self.cleaned_data)
        c.pop("note", None)
        return c
