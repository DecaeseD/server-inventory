from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from catalog.models import Category, ItemType
from inventory import services as S
from inventory.models import Location, Operation, StockDocument, Unit


def formset(lines, key="key-1", note=""):
    data = {"occurred_at": "2026-10-01T10:00", "note": note, "idempotency_key": key,
            "form-TOTAL_FORMS": len(lines), "form-INITIAL_FORMS": 0,
            "form-MIN_NUM_FORMS": 1, "form-MAX_NUM_FORMS": 1000}
    for i, l in enumerate(lines):
        for k, v in l.items():
            data[f"form-{i}-{k}"] = v
    return data


class PanelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from accounts.apps import create_roles
        create_roles(None)
        mk = lambda n, g: (lambda u: (u.groups.add(Group.objects.get(name=g)), u)[1])(User.objects.create_user(n, password="pw"))
        cls.keeper, cls.keeper2, cls.manager = mk("keeper", "انباردار"), mk("keeper2", "انباردار"), mk("manager", "مدیر کل")
        cls.nobody = User.objects.create_user("nobody", password="pw")
        cat = Category.objects.create(name="c")
        cls.cpu = ItemType.objects.create(name="CPU", category=cat, min_stock=5)
        cls.ram = ItemType.objects.create(name="RAM", category=cat, tracking_mode="serial")
        cls.loc = Location.objects.create(name="main")

    def login(self, u):
        self.client.force_login(u)

    def buy(self, qty=3, key="key-1", serials=""):
        item = self.ram if serials else self.cpu
        return self.client.post(reverse("panels:new_request"), formset([dict(
            item_type=item.pk, operation="purchase", quantity=qty, to_location=self.loc.pk,
            unit_price="1000.50", serials=serials)], key=key))

    def test_login_required_and_roles(self):
        self.assertEqual(self.client.get(reverse("panels:dashboard")).status_code, 302)
        self.login(self.nobody)
        self.assertEqual(self.client.get(reverse("panels:home")).status_code, 403)
        self.login(self.keeper)
        for name in ("dashboard", "items", "new_request", "my_requests"):
            self.assertEqual(self.client.get(reverse(f"panels:{name}")).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("panels:approvals")).status_code, 403)

    def test_submit_creates_pending_and_stock_unchanged(self):
        self.login(self.keeper)
        r = self.buy()
        doc = StockDocument.objects.get()
        self.assertRedirects(r, reverse("panels:document", args=[doc.pk]))
        self.assertEqual(doc.status, "pending")
        self.assertEqual(S.get_balance(self.cpu, "free"), 0)

    def test_double_submit_same_key_creates_one_document(self):
        self.login(self.keeper)
        self.buy(); self.buy()
        self.assertEqual(StockDocument.objects.count(), 1)

    def test_validation_error_shown_and_nothing_saved(self):
        self.login(self.keeper)
        r = self.client.post(reverse("panels:new_request"), formset([dict(
            item_type=self.ram.pk, operation="purchase", quantity=2, to_location=self.loc.pk, serials="A")]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "شماره سریال")
        self.assertEqual(StockDocument.objects.count(), 0)

    def test_keeper_cannot_see_others_documents_or_decide(self):
        self.login(self.keeper); self.buy()
        doc = StockDocument.objects.get()
        self.login(self.keeper2)
        self.assertEqual(self.client.get(reverse("panels:document", args=[doc.pk])).status_code, 404)
        self.login(self.keeper)
        self.assertEqual(self.client.post(reverse("panels:approve", args=[doc.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("panels:reject", args=[doc.pk]), {"reason": "x"}).status_code, 403)
        doc.refresh_from_db(); self.assertEqual(doc.status, "pending")

    def test_prices_hidden_from_keeper_visible_to_manager(self):
        self.login(self.keeper); self.buy()
        doc = StockDocument.objects.get()
        self.assertNotContains(self.client.get(reverse("panels:document", args=[doc.pk])), "1000.50")
        self.login(self.manager)
        self.assertContains(self.client.get(reverse("panels:document", args=[doc.pk])), "1000.50")

    def test_manager_approves_reject_needs_reason_and_get_not_allowed(self):
        self.login(self.keeper); self.buy()
        doc = StockDocument.objects.get()
        self.login(self.manager)
        self.assertEqual(self.client.get(reverse("panels:approve", args=[doc.pk])).status_code, 405)
        self.client.post(reverse("panels:reject", args=[doc.pk]), {"reason": " "}, follow=True)
        doc.refresh_from_db(); self.assertEqual(doc.status, "pending")
        self.client.post(reverse("panels:approve", args=[doc.pk]))
        doc.refresh_from_db(); self.assertEqual(doc.status, "approved")
        self.assertEqual(S.get_balance(self.cpu, "free"), 3)

    def test_reject_flow_shows_reason_to_keeper(self):
        self.login(self.keeper); self.buy()
        doc = StockDocument.objects.get()
        self.login(self.manager)
        self.client.post(reverse("panels:reject", args=[doc.pk]), {"reason": "مقدار اشتباه"})
        self.login(self.keeper)
        self.assertContains(self.client.get(reverse("panels:document", args=[doc.pk])), "مقدار اشتباه")

    def test_approve_failure_is_reported_not_crashed(self):
        self.login(self.keeper)
        self.client.post(reverse("panels:new_request"), formset([dict(
            item_type=self.cpu.pk, operation="issue", quantity=9, from_location=self.loc.pk)]))
        doc = StockDocument.objects.get()
        self.login(self.manager)
        r = self.client.post(reverse("panels:approve", args=[doc.pk]), follow=True)
        self.assertContains(r, "موجودی")
        doc.refresh_from_db(); self.assertEqual(doc.status, "pending")

    def test_void_via_panel(self):
        self.login(self.keeper); self.buy()
        doc = StockDocument.objects.get()
        self.login(self.manager)
        self.client.post(reverse("panels:approve", args=[doc.pk]))
        self.client.post(reverse("panels:void", args=[doc.pk]), {"reason": "اشتباه"})
        doc.refresh_from_db(); self.assertEqual(doc.status, "voided")
        self.assertEqual(S.get_balance(self.cpu, "free"), 0)

    def test_item_search_by_serial_filter_pagination_and_detail(self):
        self.login(self.keeper); self.buy(2, "k1", "SER-AAA\nSER-BBB")
        self.login(self.manager)
        self.client.post(reverse("panels:approve", args=[StockDocument.objects.get().pk]))
        self.login(self.keeper)
        r = self.client.get(reverse("panels:items"), {"q": "SER-AAA"})
        self.assertContains(r, "RAM"); self.assertNotContains(r, ">CPU<")
        r = self.client.get(reverse("panels:item", args=[self.ram.pk]), {"u": "SER-BBB"})
        serials = [u.serial_number for u in r.context["units"]]
        self.assertEqual(serials, ["SER-BBB"])  # units table is filtered; history stays complete
        low = self.client.get(reverse("panels:items"), {"low": 1})
        self.assertContains(low, "CPU")

    def test_server_page_lists_components(self):
        srv_t = ItemType.objects.create(name="HP", category=self.cpu.category, tracking_mode="serial", is_server=True)
        keeper_doc = lambda *lines: S.create_document(user=self.keeper, lines=list(lines), idempotency_key=f"x{len(lines)}{id(lines)}")
        d1 = keeper_doc(dict(item_type=srv_t, operation="purchase", quantity=1, to_location=self.loc, serials=["S1"]),
                        dict(item_type=self.cpu, operation="purchase", quantity=2, to_location=self.loc))
        S.approve_document(d1.pk, self.manager)
        srv = Unit.objects.get(serial_number="S1")
        d2 = keeper_doc(dict(item_type=self.cpu, operation="install", quantity=2, from_location=self.loc, server=srv))
        S.approve_document(d2.pk, self.manager)
        self.login(self.keeper)
        r = self.client.get(reverse("panels:unit", args=[srv.pk]))
        self.assertContains(r, "CPU"); self.assertContains(r, "قطعات فعلی")


import json
import re
from django.test import Client
from inventory.models import Bucket


class FixesTests(TestCase):
    """Regression tests for the issues found in manual testing."""

    def test_roles_exist_after_plain_migrate(self):
        # deliberately NOT calling create_roles(): migrate itself must create them
        names = set(Group.objects.values_list("name", flat=True))
        self.assertTrue({"مدیر کل", "انباردار", "حسابدار"} <= names)
        self.assertTrue(Group.objects.get(name="انباردار").permissions.filter(codename="submit_document").exists())
        self.assertEqual(Group.objects.get(name="حسابدار").permissions.count(), 0)

    def test_ops_meta_is_json_object_with_location_flags(self):
        u = User.objects.create_user("k"); u.groups.add(Group.objects.get(name="انباردار"))
        c = Client(); c.force_login(u)
        html = c.get(reverse("panels:new_request")).content.decode()
        meta = json.loads(re.search(r'id="ops-meta"[^>]*>(.*?)</script>', html, re.S).group(1))
        self.assertIsInstance(meta, dict)
        self.assertTrue(meta["ops"]["purchase"]["to"] and meta["ops"]["purchase"]["price"])
        self.assertFalse(meta["ops"]["dc_return"]["price"])
        self.assertTrue(meta["ops"]["enter_installed"]["server"])
        self.assertIn('class="fld f-to_location"', html)

    def test_login_works_with_csrf_enforced(self):
        User.objects.create_user("u1", password="pw12345!")
        c = Client(enforce_csrf_checks=True)
        page = c.get(reverse("panels:login"))
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        r = c.post(reverse("panels:login"), {"username": "u1", "password": "pw12345!",
                                              "csrfmiddlewaretoken": token})
        self.assertEqual(r.status_code, 302)

    def test_user_without_role_gets_friendly_403_with_logout(self):
        u = User.objects.create_user("norole")
        c = Client(); c.force_login(u)
        r = c.get(reverse("panels:home"))
        self.assertEqual(r.status_code, 403)
        self.assertContains(r, "دسترسی ندارید", status_code=403)
        self.assertContains(r, "خروج", status_code=403)


class ServerComponentsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cat = Category.objects.create(name="c")
        cls.srv_t = ItemType.objects.create(name="HP G10", category=cat, tracking_mode="serial", is_server=True)
        cls.ram = ItemType.objects.create(name="RAM", category=cat, tracking_mode="serial")
        cls.cpu = ItemType.objects.create(name="CPU", category=cat)
        cls.loc = Location.objects.create(name="main")
        cls.keeper = User.objects.create_user("kp"); cls.keeper.groups.add(Group.objects.get(name="انباردار"))
        cls.manager = User.objects.create_user("mg"); cls.manager.groups.add(Group.objects.get(name="مدیر کل"))
        cls.n = 0

    def doc(self, *lines):
        ServerComponentsTests.n += 1
        return S.create_document(user=self.keeper, lines=list(lines), idempotency_key=f"sc{self.n}")

    def server_with_parts(self):
        return self.doc(
            dict(item_type=self.srv_t, operation="purchase", quantity=1, to_location=self.loc, serials=["SRV1"]),
            dict(item_type=self.ram, operation="enter_installed", quantity=2, server_serial="SRV1", serials=["R1", "R2"], unit_price="10"),
            dict(item_type=self.cpu, operation="enter_installed", quantity=2, server_serial="SRV1"))

    def test_server_enters_with_its_parts_and_nothing_is_double_counted(self):
        d = self.server_with_parts()
        S.approve_document(d.pk, self.manager)
        srv = Unit.objects.get(serial_number="SRV1")
        self.assertEqual(sorted(S.components_of(srv)), sorted([(self.ram.pk, 2), (self.cpu.pk, 2)]))
        self.assertEqual(S.get_balance(self.ram, Bucket.FREE), 0)       # parts are NOT free stock
        self.assertEqual(S.get_balance(self.ram, Bucket.INSTALLED), 2)
        self.assertEqual(S.get_balance(self.srv_t, Bucket.FREE), 1)
        self.assertEqual(Unit.objects.get(serial_number="R1").installed_in_id, srv.pk)
        self.assertEqual(S.find_inconsistencies(), [])

    def test_voiding_server_document_reverses_parts_first(self):
        d = self.server_with_parts()
        S.approve_document(d.pk, self.manager)
        S.void_document(d.pk, self.manager, "اشتباه")
        for item in (self.srv_t, self.ram, self.cpu):
            self.assertEqual(sum(S.stock_summary(item).values()), 0)
        self.assertEqual(S.find_inconsistencies(), [])

    def test_unknown_server_serial_rejected_and_order_matters(self):
        with self.assertRaises(S.InventoryError):
            self.doc(dict(item_type=self.cpu, operation="enter_installed", quantity=1, server_serial="NOPE"))
        with self.assertRaises(S.InventoryError):  # part listed before its server
            self.doc(dict(item_type=self.cpu, operation="enter_installed", quantity=1, server_serial="SRV9"),
                     dict(item_type=self.srv_t, operation="purchase", quantity=1, to_location=self.loc, serials=["SRV9"]))

    def test_server_with_parts_cannot_be_scrapped_until_parts_removed(self):
        S.approve_document(self.server_with_parts().pk, self.manager)
        srv = Unit.objects.get(serial_number="SRV1")
        d = self.doc(dict(item_type=self.srv_t, operation="scrap", quantity=1, from_location=self.loc,
                          to_location=self.loc, serials=["SRV1"]))
        with self.assertRaises(S.InventoryError):
            S.approve_document(d.pk, self.manager)
        self.assertEqual(Unit.objects.get(pk=srv.pk).status, "free")

    def test_price_ignored_for_non_financial_operations(self):
        d = self.doc(dict(item_type=self.cpu, operation="dc_return", quantity=1, from_location=self.loc,
                          to_location=self.loc, unit_price="999"))
        self.assertIsNone(d.lines.get().unit_price)

    def test_panel_post_with_new_server_and_parts(self):
        c = Client(); c.force_login(self.keeper)
        lines = [
            dict(item_type=self.srv_t.pk, operation="purchase", quantity=1, to_location=self.loc.pk, serials="SRVX"),
            dict(item_type=self.ram.pk, operation="enter_installed", quantity=2, server_serial="SRVX", serials="A1\nA2"),
        ]
        r = c.post(reverse("panels:new_request"), formset(lines, key="panel-srv"))
        self.assertEqual(r.status_code, 302)
        doc = StockDocument.objects.get(idempotency_key="panel-srv")
        self.assertEqual(doc.lines.count(), 2)
        S.approve_document(doc.pk, self.manager)
        self.assertEqual(S.get_balance(self.ram, "installed"), 2)
