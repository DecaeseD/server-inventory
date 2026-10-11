import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Sum
from django.forms import formset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from catalog.models import Category, ItemType
from inventory import services as S
from inventory.models import (OPERATION_MOVES, PRICED_OPERATIONS, SERVER_OPERATIONS, Bucket,
                              LedgerEntry, Location, StockDocument, Unit, UnitStatus)

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


def _active_servers():
    return (Unit.objects.filter(item_type__is_server=True)
            .exclude(status__in=[UnitStatus.OUT, UnitStatus.SCRAPPED])
            .select_related("item_type").order_by("serial_number"))


@perm("catalog.view_itemtype")
def item_detail(request, pk):
    item = get_object_or_404(ItemType.objects.select_related("category")
                             .prefetch_related("attributes__definition"), pk=pk)
    summary = S.stock_summary(item)
    units = item.units.select_related("location", "installed_in", "installed_in__item_type")
    uq = request.GET.get("u", "").strip()
    if uq:
        units = units.filter(Q(serial_number__icontains=uq) | Q(asset_code__icontains=uq))
    ustatus = request.GET.get("ustatus")
    units = units.filter(status=ustatus) if ustatus else units.exclude(status=UnitStatus.OUT)
    history = (LedgerEntry.objects.filter(item_type=item)
               .select_related("document", "line", "location", "server", "unit")
               .order_by("-occurred_at", "-id"))
    legacy = {r["bucket"]: r["t"] for r in LedgerEntry.objects.filter(item_type=item, unit__isnull=True)
              .values("bucket").annotate(t=Sum("delta")) if r["t"]}
    can_act = request.user.has_perm("inventory.submit_document") and not item.is_server
    return render(request, "panels/item_detail.html", {
        "item": item,
        "summary": [(b.value, b.label, summary[b.value]) for b in Bucket],
        "free_by_location": _free_by_location(item),
        "units": paginate(request, units.order_by("serial_number", "pk"), 25, "upage"),
        "history": paginate(request, history, 20, "page"),
        "unit_statuses": list(Unit._meta.get_field("status").choices),
        "legacy": [(dict(Bucket.choices).get(b, b), q) for b, q in legacy.items()],
        "can_act": can_act,
        "locations": Location.objects.filter(is_active=True).order_by("name"),
        "servers": _active_servers() if can_act else [],
    })


def _free_by_location(item):
    from django.db.models import Sum
    rows = (LedgerEntry.objects.filter(item_type=item, bucket=Bucket.FREE)
            .values("location__name").annotate(q=Sum("delta")).order_by("location__name"))
    return [r for r in rows if r["q"]]


@perm("catalog.view_itemtype")
def unit_detail(request, pk):
    unit = get_object_or_404(Unit.objects.select_related("item_type", "location", "installed_in"), pk=pk)
    parts = (Unit.objects.filter(installed_in=unit).select_related("item_type")
             .order_by("item_type__name", "serial_number", "pk"))
    comps = []
    for item_id, qty in S.components_of(unit):
        it = ItemType.objects.get(pk=item_id)
        mine = [u for u in parts if u.item_type_id == item_id]
        comps.append({"item": it, "qty": qty, "units": mine, "unidentified": max(qty - len(mine), 0)})
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
            "enter": fb is None,
        }
    items = {i.pk: {"server": i.is_server, "mode": i.tracking_mode}
             for i in ItemType.objects.filter(is_active=True)}
    servers = [pk for pk, v in items.items() if v["server"]]
    return {"ops": ops, "servers": servers, "items": items}


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
        "head": head, "fs": fs, "ops_meta": _ops_meta(), "servers": _active_servers()})  # dict: json_script encodes it once


@perm("inventory.submit_document")
def my_requests(request):
    qs = StockDocument.objects.filter(created_by=request.user).select_related("decided_by")
    if request.GET.get("status") in StockDocument.Status.values:
        qs = qs.filter(status=request.GET["status"])
    return render(request, "panels/my_requests.html", {
        "page": paginate(request, qs.order_by("-id")),
        "statuses": StockDocument.Status.choices})


# ---------------------------------------------------------------- actions from the item page
def _find_target_server(text):
    text = (text or "").strip()
    found = list(_active_servers().filter(serial_number=text)[:2]) if text else []
    if len(found) != 1:
        raise S.InventoryError("سرور مقصد پیدا نشد؛ سریال را دقیق از لیست انتخاب کن." if not found
                               else "چند سرور با این سریال وجود دارد.")
    return found[0]


@require_POST
@perm("inventory.submit_document")
def unit_action(request, pk):
    """Register a (pending) request for the selected pieces of one item:
    uninstall from server / move to another server / install a free piece on a server."""
    item = get_object_or_404(ItemType, pk=pk)
    back = redirect("panels:item", pk)
    units = list(Unit.objects.filter(item_type=item, pk__in=request.POST.getlist("units"))
                 .select_related("location", "installed_in").order_by("pk"))
    if not units:
        messages.error(request, "هیچ نمونه‌ای انتخاب نشده است.")
        return back
    action = request.POST.get("action")
    loc = Location.objects.filter(pk=request.POST.get("location") or 0, is_active=True).first()
    try:
        lines = []
        if action in ("uninstall", "move"):
            if any(u.status != UnitStatus.INSTALLED for u in units):
                raise S.InventoryError("فقط قطعاتِ نصب‌شده روی سرور را می‌شود جدا یا منتقل کرد.")
            if loc is None:
                raise S.InventoryError("انبار مقصد را انتخاب کن.")
            groups = {}
            for u in units:
                groups.setdefault(u.installed_in, []).append(u)
            for srv, grp in groups.items():
                lines.append(dict(item_type=item, operation="uninstall", quantity=len(grp),
                                  to_location=loc, server=srv, serials=[u.ident for u in grp]))
            if action == "move":
                target = _find_target_server(request.POST.get("target_server"))
                if target in groups:
                    raise S.InventoryError("بعضی از قطعات انتخاب‌شده همین الان روی سرور مقصد هستند.")
                lines.append(dict(item_type=item, operation="install", quantity=len(units),
                                  from_location=loc, server=target, serials=[u.ident for u in units]))
        elif action == "install":
            if any(u.status != UnitStatus.FREE for u in units):
                raise S.InventoryError("فقط نمونه‌های آزاد را می‌شود روی سرور نصب کرد.")
            target = _find_target_server(request.POST.get("target_server"))
            groups = {}
            for u in units:
                groups.setdefault(u.location, []).append(u)
            for place, grp in groups.items():
                lines.append(dict(item_type=item, operation="install", quantity=len(grp),
                                  from_location=place, server=target, serials=[u.ident for u in grp]))
        else:
            raise S.InventoryError("عملیات نامعتبر است.")
        doc = S.create_document(user=request.user, lines=lines, idempotency_key=uuid.uuid4().hex,
                                note=request.POST.get("note", "")[:500])
    except S.InventoryError as e:
        messages.error(request, str(e))
        return back
    messages.success(request, f"درخواست #{doc.pk} ثبت شد و منتظر تأیید مدیر است.")
    return redirect("panels:document", doc.pk)


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
