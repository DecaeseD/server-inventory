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
    item_type = ItemChoice(label="کالا", queryset=ItemType.objects.none())
    operation = forms.ChoiceField(
        label="عملیات", choices=[(v, l) for v, l in Operation.choices if v != Operation.REVERSAL])
    quantity = forms.IntegerField(label="تعداد", min_value=1)
    unit_price = forms.DecimalField(label="قیمت واحد", required=False, min_value=0,
                                    max_digits=16, decimal_places=2)
    from_location = forms.ModelChoiceField(label="محل مبدأ", required=False,
                                           queryset=Location.objects.none())
    to_location = forms.ModelChoiceField(label="محل مقصد", required=False,
                                         queryset=Location.objects.none())
    server = forms.ModelChoiceField(label="سرور", required=False, queryset=Unit.objects.none())
    server_serial = forms.CharField(
        label="یا سریال سرورِ جدید همین درخواست", required=False, max_length=100,
        help_text="اگر سرور هنوز ثبت نشده و در همین درخواست وارد می‌شود، سریالش را بنویس.")
    serials = forms.CharField(label="شماره سریال‌ها (هر سطر یکی)", required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    note = forms.CharField(label="توضیح قلم", required=False, max_length=255)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item_type"].queryset = ItemType.objects.filter(is_active=True).order_by("name")
        locs = Location.objects.filter(is_active=True).order_by("name")
        self.fields["from_location"].queryset = locs
        self.fields["to_location"].queryset = locs
        self.fields["server"].queryset = (
            Unit.objects.filter(item_type__is_server=True)
            .exclude(status__in=[UnitStatus.OUT, UnitStatus.SCRAPPED])
            .select_related("item_type").order_by("item_type__name", "label", "serial_number"))

    def to_line_dict(self):
        c = self.cleaned_data
        serials = [s.strip() for s in re.split(r"[\n,،]+", c.get("serials") or "") if s.strip()]
        return dict(item_type=c["item_type"], operation=c["operation"], quantity=c["quantity"],
                    unit_price=c.get("unit_price"), from_location=c.get("from_location"),
                    to_location=c.get("to_location"), server=c.get("server"),
                    server_serial=c.get("server_serial", ""),
                    serials=serials, note=c.get("note", ""))
