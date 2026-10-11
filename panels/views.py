import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.forms import formset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from catalog.models import Category, ItemType
from inventory import services as S
from inventory.models import (OPERATION_MOVES, PRICED_OPERATIONS, SERVER_OPERATIONS, Bucket,
                              LedgerEntry, StockDocument, Unit)

from .forms import DocumentForm, LineForm


def perm(p):
    def deco(f):
        return login_required(permission_required(p, raise_exception=True)(f))
    return deco


def paginate(request, qs, per=20, param="page"):
    return Paginator(qs, per).get_page(request.GET.get(param))


def _can_see_all_docs(user):
    return user.has_perm("inventory.approve_document") or user.has_perm("inventory.view_stockdocument")


# ---------------------------------------------------------------- entry points
@login_required
def home(request):
    u = request.user
    if u.has_perm("inventory.approve_document"):
        return redirect("panels:approvals")
    if u.has_perm("catalog.view_itemtype") or u.has_perm("inventory.submit_document"):
        return redirect("panels:dashboard")
    raise PermissionDenied("برای این حساب نقشی تعریف نشده است. با مدیر تماس بگیرید.")


@perm("catalog.view_itemtype")
def dashboard(request):
    mine = StockDocument.objects.filter(created_by=request.user)
    counts = {r["status"]: r["n"] for r in mine.values("status").annotate(n=Count("id"))}
    low = (S.stock_table(ItemType.objects.filter(is_active=True, min_stock__gt=0))
           .filter(qty_free__lt=F("min_stock")).order_by("name")[:10])
    return render(request, "panels/dashboard.html", {
        "counts": counts, "low": low,
        "recent": mine.order_by("-id")[:5],
    })


# ---------------------------------------------------------------- items
@perm("catalog.view_itemtype")
def item_list(request):
    qs = S.stock_table(ItemType.objects.select_related("category"))
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(brand__icontains=q) | Q(model_name__icontains=q)
                       | Q(sku__icontains=q)
                       | Q(pk__in=Unit.objects.filter(serial_number__icontains=q).values("item_type")))
    if request.GET.get("category"):
        qs = qs.filter(category_id=request.GET["category"])
    if request.GET.get("tracking") in ItemType.Tracking.values:
        qs = qs.filter(tracking_mode=request.GET["tracking"])
    if request.GET.get("low"):
        qs = qs.filter(min_stock__gt=0, qty_free__lt=F("min_stock"))
    if not request.GET.get("inactive"):
        qs = qs.filter(is_active=True)
    return render(request, "panels/item_list.html", {
        "page": paginate(request, qs.order_by("name")),
        "categories": Category.objects.order_by("name"),
        "tracking_choices": ItemType.Tracking.choices,
    })


@perm("catalog.view_itemtype")
def item_detail(request, pk):
    item = get_object_or_404(ItemType.objects.select_related("category")
                             .prefetch_related("attributes__definition"), pk=pk)
    summary = S.stock_summary(item)
    units = item.units.exclude(status="out").select_related("location", "installed_in")
    uq = request.GET.get("u", "").strip()
    if uq:
        units = units.filter(serial_number__icontains=uq)
    if request.GET.get("ustatus"):
        units = units.filter(status=request.GET["ustatus"])
    history = (LedgerEntry.objects.filter(item_type=item)
               .select_related("document", "line", "location", "server", "unit")
               .order_by("-occurred_at", "-id"))
    return render(request, "panels/item_detail.html", {
        "item": item,
        "summary": [(b.label, summary[b.value]) for b in Bucket],
        "free_by_location": _free_by_location(item),
        "units": paginate(request, units.order_by("serial_number", "pk"), 25, "upage"),
        "history": paginate(request, history, 20, "page"),
        "unit_statuses": [c for c in Unit._meta.get_field("status").choices if c[0] != "out"],
    })


def _free_by_location(item):
    from django.db.models import Sum
    rows = (LedgerEntry.objects.filter(item_type=item, bucket=Bucket.FREE)
            .values("location__name").annotate(q=Sum("delta")).order_by("location__name"))
    return [r for r in rows if r["q"]]


@perm("catalog.view_itemtype")
def unit_detail(request, pk):
    unit = get_object_or_404(Unit.objects.select_related("item_type", "location", "installed_in"), pk=pk)
    comps = []
    for item_id, qty in S.components_of(unit):
        it = ItemType.objects.get(pk=item_id)
        serials = list(Unit.objects.filter(installed_in=unit, item_type=it)
                       .order_by("serial_number").values_list("serial_number", flat=True))
        comps.append({"item": it, "qty": qty, "serials": serials})
    history = (LedgerEntry.objects.filter(Q(unit=unit) | Q(server=unit))
               .select_related("document", "line", "item_type", "location").order_by("-occurred_at", "-id"))
    return render(request, "panels/unit_detail.html", {
        "unit": unit, "components": comps, "history": paginate(request, history, 20)})


# ---------------------------------------------------------------- creating requests
def _ops_meta():
    ops = {}
    for op, (fb, tb) in OPERATION_MOVES.items():
        ops[op.value] = {
            "from": fb is not None and fb != Bucket.INSTALLED,
            "to": tb is not None and tb != Bucket.INSTALLED,
            "server": op in SERVER_OPERATIONS,
            "price": op in PRICED_OPERATIONS,
        }
    servers = list(ItemType.objects.filter(is_server=True, is_active=True).values_list("pk", flat=True))
    return {"ops": ops, "servers": servers}


@perm("inventory.submit_document")
def new_request(request):
    FormSet = formset_factory(LineForm, extra=0, min_num=1, validate_min=True)
    if request.method == "POST":
        head, fs = DocumentForm(request.POST), FormSet(request.POST)
        if head.is_valid() and fs.is_valid():
            lines, ok = [], True
            for form in fs:
                if not form.cleaned_data:
                    continue
                d = form.to_line_dict()
                try:
                    S.validate_line(d)
                except S.InventoryError as e:
                    form.add_error(None, str(e))
                    ok = False
                lines.append(d)
            if ok and lines:
                try:
                    doc = S.create_document(user=request.user, lines=lines,
                                            idempotency_key=head.cleaned_data["idempotency_key"],
                                            occurred_at=head.cleaned_data["occurred_at"],
                                            note=head.cleaned_data["note"])
                except S.InventoryError as e:
                    head.add_error(None, str(e))
                else:
                    messages.success(request, f"درخواست #{doc.pk} ثبت شد و منتظر تأیید مدیر است.")
                    return redirect("panels:document", doc.pk)
    else:
        head = DocumentForm(initial={
            "idempotency_key": uuid.uuid4().hex,
            "occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M")})
        fs = FormSet()
    return render(request, "panels/request_form.html", {
        "head": head, "fs": fs, "ops_meta": _ops_meta()})  # dict: json_script encodes it once


@perm("inventory.submit_document")
def my_requests(request):
    qs = StockDocument.objects.filter(created_by=request.user).select_related("decided_by")
    if request.GET.get("status") in StockDocument.Status.values:
        qs = qs.filter(status=request.GET["status"])
    return render(request, "panels/my_requests.html", {
        "page": paginate(request, qs.order_by("-id")),
        "statuses": StockDocument.Status.choices})


# ---------------------------------------------------------------- documents
@login_required
def document_detail(request, pk):
    qs = StockDocument.objects.select_related("created_by", "decided_by", "reverses")
    if not _can_see_all_docs(request.user):
        qs = qs.filter(created_by=request.user)  # IDOR guard: others' documents => 404
    doc = get_object_or_404(qs, pk=pk)
    lines = list(doc.lines.select_related("item_type", "from_location", "to_location", "server")
                 .order_by("pk"))
    can_approve = request.user.has_perm("inventory.approve_document")
    if doc.status == StockDocument.Status.PENDING and can_approve:
        for l in lines:
            l.impact = S.preview_line(l)
    return render(request, "panels/document.html", {
        "doc": doc, "lines": lines, "can_approve": can_approve,
        "can_void": request.user.has_perm("inventory.void_document"),
        "show_prices": request.user.has_perm("inventory.view_prices"),
    })


def _decide(request, pk, fn, ok_msg):
    try:
        fn()
        messages.success(request, ok_msg)
    except S.InventoryError as e:
        messages.error(request, str(e))
    return redirect("panels:document", pk)


@require_POST
@perm("inventory.approve_document")
def document_approve(request, pk):
    return _decide(request, pk, lambda: S.approve_document(pk, request.user, ip=request.META.get("REMOTE_ADDR")),
                   "درخواست تأیید شد و موجودی اعمال شد.")


@require_POST
@perm("inventory.approve_document")
def document_reject(request, pk):
    return _decide(request, pk, lambda: S.reject_document(
        pk, request.user, request.POST.get("reason", ""), ip=request.META.get("REMOTE_ADDR")),
        "درخواست رد شد.")


@require_POST
@perm("inventory.void_document")
def document_void(request, pk):
    return _decide(request, pk, lambda: S.void_document(
        pk, request.user, request.POST.get("reason", ""), ip=request.META.get("REMOTE_ADDR")),
        "سند باطل شد و سند معکوس ثبت شد.")


@perm("inventory.approve_document")
def approvals(request):
    qs = StockDocument.objects.select_related("created_by", "decided_by")
    status = request.GET.get("status", "pending")
    if status in StockDocument.Status.values:
        qs = qs.filter(status=status)
    q = request.GET.get("q", "").strip()
    if q:
        cond = Q(created_by__username__icontains=q) | Q(note__icontains=q) | Q(
            lines__item_type__name__icontains=q)
        if q.isdigit():
            cond |= Q(pk=int(q))
        qs = qs.filter(cond).distinct()
    if request.GET.get("date_from"):
        qs = qs.filter(created_at__date__gte=request.GET["date_from"])
    if request.GET.get("date_to"):
        qs = qs.filter(created_at__date__lte=request.GET["date_to"])
    return render(request, "panels/approvals.html", {
        "page": paginate(request, qs.order_by("-id")),
        "statuses": StockDocument.Status.choices, "status": status,
        "pending_count": StockDocument.objects.filter(status="pending").count()})


def csrf_failure(request, reason=""):
    return render(request, "403_csrf.html", status=403)
