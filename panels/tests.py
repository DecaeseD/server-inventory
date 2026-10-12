from unittest import mock

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
        r = self.client.post(reverse("panels:new_request"), formset([dict(
            item_type=self.cpu.pk, operation="issue", quantity=9, from_location=self.loc.pk)]))
        self.assertContains(r, "موجودی")                      # refused already at submit time
        self.assertEqual(StockDocument.objects.count(), 0)
        with mock.patch.object(S, "_dry_run"):                # stock vanished after submit => approval refuses
            self.client.post(reverse("panels:new_request"), formset([dict(
                item_type=self.cpu.pk, operation="issue", quantity=9, from_location=self.loc.pk)], key="k-late"))
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
        self.assertContains(r, "CPU"); self.assertContains(r, "مشخصات سخت‌افزاری")


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
        # a part typed BEFORE its (new) server is fine: the system puts the server first
        d = self.doc(dict(item_type=self.cpu, operation="enter_installed", quantity=1, server_serial="SRV9"),
                     dict(item_type=self.srv_t, operation="purchase", quantity=1, to_location=self.loc, serials=["SRV9"]))
        S.approve_document(d.pk, self.manager)
        self.assertEqual(S.get_balance(self.cpu, "installed"), 1)

    def test_server_with_parts_cannot_be_scrapped_until_parts_removed(self):
        S.approve_document(self.server_with_parts().pk, self.manager)
        srv = Unit.objects.get(serial_number="SRV1")
        with self.assertRaises(S.InventoryError):
            self.doc(dict(item_type=self.srv_t, operation="scrap", quantity=1, from_location=self.loc,
                          to_location=self.loc, serials=["SRV1"]))
        self.assertEqual(Unit.objects.get(pk=srv.pk).status, "free")

    def test_price_ignored_for_non_financial_operations(self):
        S.approve_document(self.doc(dict(item_type=self.cpu, operation="purchase", quantity=1, to_location=self.loc),
                                    dict(item_type=self.cpu, operation="send_dc", quantity=1, from_location=self.loc,
                                         to_location=self.loc)).pk, self.manager)
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


class ItemPageActionsTests(TestCase):
    """Uninstall / move / install straight from the item page (all create PENDING requests)."""

    @classmethod
    def setUpTestData(cls):
        from accounts.apps import create_roles
        create_roles(None)
        cat = Category.objects.create(name="c")
        cls.srv_t = ItemType.objects.create(name="HP G10", category=cat, tracking_mode="serial", is_server=True)
        cls.ram = ItemType.objects.create(name="RAM", category=cat, tracking_mode="serial")
        cls.cpu = ItemType.objects.create(name="CPU", category=cat)
        cls.main = Location.objects.create(name="main")
        cls.keeper = User.objects.create_user("kp"); cls.keeper.groups.add(Group.objects.get(name="انباردار"))
        cls.manager = User.objects.create_user("mg"); cls.manager.groups.add(Group.objects.get(name="مدیر کل"))
        cls.viewer = User.objects.create_user("vw")
        cls.viewer.user_permissions.add(*__import__("django.contrib.auth.models", fromlist=["Permission"])
                                        .Permission.objects.filter(codename="view_itemtype"))

    def setUp(self):
        n = [0]
        def go(*lines):
            n[0] += 1
            d = S.create_document(user=self.keeper, lines=list(lines), idempotency_key=f"s{n[0]}")
            S.approve_document(d.pk, self.manager)
        go(dict(item_type=self.srv_t, operation="purchase", quantity=2, to_location=self.main, serials=["A", "B"]),
           dict(item_type=self.ram, operation="purchase", quantity=3, to_location=self.main, serials=["R1", "R2", "R3"]),
           dict(item_type=self.cpu, operation="purchase", quantity=3, to_location=self.main))
        self.a, self.b = Unit.objects.get(serial_number="A"), Unit.objects.get(serial_number="B")
        go(dict(item_type=self.ram, operation="install", quantity=2, from_location=self.main, server=self.a, serials=["R1", "R2"]),
           dict(item_type=self.cpu, operation="install", quantity=2, from_location=self.main, server=self.a))
        self.client.force_login(self.keeper)

    def act(self, item, units, **data):
        return self.client.post(reverse("panels:unit_action", args=[item.pk]),
                                dict(units=[u.pk for u in units], **data))

    def approve_last(self):
        S.approve_document(StockDocument.objects.latest("id").pk, self.manager)

    def test_uninstall_two_rams_back_to_free_stock(self):
        rams = list(Unit.objects.filter(item_type=self.ram, status="installed"))
        r = self.act(self.ram, rams, action="uninstall", location=self.main.pk)
        doc = StockDocument.objects.latest("id")
        self.assertRedirects(r, reverse("panels:document", args=[doc.pk]))
        self.assertEqual(doc.status, "pending")
        self.assertEqual(S.get_balance(self.ram, "installed"), 2)            # nothing moves before approval
        self.approve_last()
        self.assertEqual(S.get_balance(self.ram, "installed"), 0)
        self.assertEqual(S.get_balance(self.ram, "free"), 3)
        self.assertEqual(S.find_inconsistencies(), [])

    def test_move_rams_to_another_server(self):
        rams = list(Unit.objects.filter(item_type=self.ram, status="installed"))
        self.act(self.ram, rams, action="move", location=self.main.pk, target_server="B")
        self.approve_last()
        self.assertEqual(S.get_balance(self.ram, "installed", server=self.b), 2)
        self.assertEqual(S.get_balance(self.ram, "installed", server=self.a), 0)
        self.assertEqual(S.find_inconsistencies(), [])

    def test_quantity_parts_can_be_picked_individually(self):
        cpus = list(Unit.objects.filter(item_type=self.cpu, status="installed"))[:1]
        self.act(self.cpu, cpus, action="uninstall", location=self.main.pk)
        self.approve_last()
        self.assertEqual(S.get_balance(self.cpu, "installed", server=self.a), 1)
        self.assertEqual(S.get_balance(self.cpu, "free"), 2)
        cpus[0].refresh_from_db(); self.assertEqual(cpus[0].status, "free")

    def test_install_free_piece_on_server(self):
        free = list(Unit.objects.filter(item_type=self.ram, status="free"))
        self.act(self.ram, free, action="install", target_server="B")
        self.approve_last()
        self.assertEqual(S.get_balance(self.ram, "installed", server=self.b), 1)

    def test_bad_input_is_refused_without_creating_requests(self):
        before = StockDocument.objects.count()
        rams = list(Unit.objects.filter(item_type=self.ram, status="installed"))
        free = list(Unit.objects.filter(item_type=self.ram, status="free"))
        self.act(self.ram, [], action="uninstall", location=self.main.pk)              # nothing selected
        self.act(self.ram, rams, action="uninstall")                                    # no destination
        self.act(self.ram, rams, action="move", location=self.main.pk, target_server="NOPE")
        self.act(self.ram, rams, action="move", location=self.main.pk, target_server="A")  # already there
        self.act(self.ram, free, action="uninstall", location=self.main.pk)            # not installed
        self.act(self.ram, rams, action="install", target_server="B")                  # not free
        self.act(self.ram, rams, action="bogus")
        self.assertEqual(StockDocument.objects.count(), before)

    def test_only_units_of_this_item_are_touched(self):
        cpu = Unit.objects.filter(item_type=self.cpu, status="installed").first()
        before = StockDocument.objects.count()
        self.act(self.ram, [cpu], action="uninstall", location=self.main.pk)
        self.assertEqual(StockDocument.objects.count(), before)

    def test_permissions(self):
        self.client.force_login(self.viewer)
        r = self.act(self.ram, list(Unit.objects.filter(item_type=self.ram))[:1], action="uninstall", location=self.main.pk)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get(reverse("panels:unit_action", args=[self.ram.pk])).status_code, 405)  # POST only
        page = self.client.get(reverse("panels:item", args=[self.ram.pk]))
        self.assertNotContains(page, 'id="bar"')                                       # no action bar without permission
        self.assertNotContains(page, 'name="units"')

    def test_item_page_shows_units_of_quantity_items_with_codes(self):
        r = self.client.get(reverse("panels:item", args=[self.cpu.pk]))
        self.assertContains(r, "3-00001".replace("3", str(self.cpu.pk), 1))
        self.assertContains(r, 'id="bar"')
        self.assertContains(r, 'name="units"')


class RequestFormHardwareTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from accounts.apps import create_roles
        create_roles(None)
        cat = Category.objects.create(name="c")
        cls.srv_t = ItemType.objects.create(name="HP G10", category=cat, tracking_mode="serial", is_server=True)
        cls.ram = ItemType.objects.create(name="RAM", category=cat, tracking_mode="serial")
        cls.main = Location.objects.create(name="main")
        cls.other = Location.objects.create(name="dc")
        cls.keeper = User.objects.create_user("kp"); cls.keeper.groups.add(Group.objects.get(name="انباردار"))
        cls.manager = User.objects.create_user("mg"); cls.manager.groups.add(Group.objects.get(name="مدیر کل"))
        d = S.create_document(user=cls.keeper, idempotency_key="seed", lines=[
            dict(item_type=cls.srv_t, operation="purchase", quantity=1, to_location=cls.main, serials=["esfand"]),
            dict(item_type=cls.ram, operation="purchase", quantity=2, to_location=cls.main, serials=["R1", "R2"])])
        S.approve_document(d.pk, cls.manager)

    def setUp(self):
        self.client.force_login(self.keeper)

    def test_unknown_server_in_panel_shows_error_and_creates_nothing(self):
        r = self.client.post(reverse("panels:new_request"), formset([dict(
            item_type=self.srv_t.pk, operation="send_dc", quantity=1, from_location=self.main.pk,
            to_location=self.other.pk, serials="taha")], key="k-taha"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "taha")
        self.assertEqual(StockDocument.objects.filter(idempotency_key="k-taha").count(), 0)

    def test_single_server_serial_field_and_datalist(self):
        r = self.client.get(reverse("panels:new_request"))
        self.assertContains(r, 'list="server-serials"')
        self.assertContains(r, '<option value="esfand">')
        self.assertNotContains(r, 'name="form-0-server"')

    def test_send_server_with_hardware_in_one_request(self):
        lines = [
            dict(item_type=self.srv_t.pk, operation="send_dc", quantity=1, from_location=self.main.pk,
                 to_location=self.other.pk, serials="esfand"),
            dict(item_type=self.ram.pk, operation="install", quantity=2, from_location=self.main.pk,
                 server_serial="esfand", serials="R1\nR2")]
        r = self.client.post(reverse("panels:new_request"), formset(lines, key="k-hw"))
        self.assertEqual(r.status_code, 302)
        doc = StockDocument.objects.get(idempotency_key="k-hw")
        self.assertEqual(doc.lines.get(operation="install").server.serial_number, "esfand")  # serial resolved to the unit
        S.approve_document(doc.pk, self.manager)
        srv = Unit.objects.get(serial_number="esfand")
        self.assertEqual((srv.status, S.components_of(srv)), ("at_datacenter", [(self.ram.pk, 2)]))

    def test_issue_server_with_hardware_listed_after_it(self):
        lines = [
            dict(item_type=self.srv_t.pk, operation="issue", quantity=1, from_location=self.main.pk, serials="esfand"),
            dict(item_type=self.ram.pk, operation="install", quantity=2, from_location=self.main.pk,
                 server_serial="esfand", serials="R1\nR2")]
        self.assertEqual(self.client.post(reverse("panels:new_request"), formset(lines, key="k-iss")).status_code, 302)
        doc = StockDocument.objects.get(idempotency_key="k-iss")
        S.approve_document(doc.pk, self.manager)
        self.assertEqual(Unit.objects.get(serial_number="R1").status, "out")
        self.assertEqual(Unit.objects.get(serial_number="esfand").status, "out")
        self.assertEqual(S.find_inconsistencies(), [])
        page = self.client.get(reverse("panels:document", args=[doc.pk]))
        self.assertContains(page, "همراه سرور")


class EditsAndServerPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from accounts.apps import create_roles
        from catalog.models import Category as C
        create_roles(None)
        c_srv, c_ram, c_disk = C.objects.create(name="Servers"), C.objects.create(name="RAM"), C.objects.create(name="Disk")
        cls.srv_t = ItemType.objects.create(name="HP G10", category=c_srv, tracking_mode="serial", is_server=True)
        cls.r32 = ItemType.objects.create(name="RAM 32", category=c_ram, tracking_mode="serial", capacity=32, capacity_unit="GB")
        cls.r64 = ItemType.objects.create(name="RAM 64", category=c_ram, tracking_mode="serial", capacity=64, capacity_unit="GB")
        cls.d6 = ItemType.objects.create(name="HDD 600", category=c_disk, capacity=600, capacity_unit="GB")
        cls.main = Location.objects.create(name="main")
        cls.keeper = User.objects.create_user("kp"); cls.keeper.groups.add(Group.objects.get(name="انباردار"))
        cls.manager = User.objects.create_user("mg"); cls.manager.groups.add(Group.objects.get(name="مدیر کل"))

    def setUp(self):
        n = [0]
        def go(*lines, user=None):
            n[0] += 1
            d = S.create_document(user=user or self.keeper, lines=list(lines), idempotency_key=f"e{n[0]}")
            S.approve_document(d.pk, self.manager)
        go(dict(item_type=self.srv_t, operation="purchase", quantity=1, to_location=self.main, serials=["SRV"]),
           dict(item_type=self.r32, operation="purchase", quantity=3, to_location=self.main, serials=["A", "B", "C"]),
           dict(item_type=self.r64, operation="purchase", quantity=1, to_location=self.main, serials=["BIG"]),
           dict(item_type=self.d6, operation="purchase", quantity=2, to_location=self.main))
        self.srv = Unit.objects.get(serial_number="SRV")
        go(dict(item_type=self.r32, operation="install", quantity=2, from_location=self.main, server=self.srv, serials=["A", "B"]),
           dict(item_type=self.d6, operation="install", quantity=2, from_location=self.main, server=self.srv))
        self.client.force_login(self.keeper)

    def approve_last(self):
        S.approve_document(StockDocument.objects.latest("id").pk, self.manager)

    # ---- server page
    def test_server_page_shows_totals_and_actions(self):
        r = self.client.get(reverse("panels:unit", args=[self.srv.pk]))
        self.assertContains(r, "64GB")                 # 2 x RAM 32
        self.assertContains(r, "1.2TB")                # 2 x 600GB
        self.assertContains(r, "تعویض")
        self.assertContains(r, "نصب سخت‌افزار")
        self.assertContains(r, "ویرایش")

    def test_remove_part(self):
        a = Unit.objects.get(serial_number="A")
        self.client.post(reverse("panels:server_action", args=[self.srv.pk]),
                         dict(action="remove", units=[a.pk], location=self.main.pk))
        self.approve_last()
        a.refresh_from_db(); self.assertEqual(a.status, "free")
        from inventory import specs
        self.assertEqual(specs.server_specs(self.srv)[1]["totals"] if False else
                         [g for g in specs.server_specs(self.srv) if g["name"] == "RAM"][0]["totals"], ["32GB"])

    def test_replace_part(self):
        a, big = Unit.objects.get(serial_number="A"), Unit.objects.get(serial_number="BIG")
        r = self.client.post(reverse("panels:server_action", args=[self.srv.pk]),
                             dict(action="replace", old=a.pk, new_units=[big.pk], location=self.main.pk))
        doc = StockDocument.objects.latest("id")
        self.assertRedirects(r, reverse("panels:document", args=[doc.pk]))
        self.assertEqual(doc.lines.count(), 2)
        self.approve_last()
        from inventory import specs
        ram = [g for g in specs.server_specs(self.srv) if g["name"] == "RAM"][0]
        self.assertEqual(ram["totals"], ["96GB"])      # 32 + 64
        a.refresh_from_db(); self.assertEqual(a.status, "free")

    def test_install_from_stock_and_new_part(self):
        c = Unit.objects.get(serial_number="C")
        self.client.post(reverse("panels:server_action", args=[self.srv.pk]),
                         dict(action="install_stock", units=[c.pk]))
        self.approve_last()
        self.assertEqual(S.get_balance(self.r32, "installed", server=self.srv), 3)
        self.client.post(reverse("panels:server_action", args=[self.srv.pk]),
                         dict(action="install_new", item=self.r64.pk, serials="N1", quantity=1))
        self.approve_last()
        self.assertEqual(S.get_balance(self.r64, "installed", server=self.srv), 1)
        self.assertEqual(S.find_inconsistencies(), [])

    def test_bad_server_actions_create_nothing(self):
        before = StockDocument.objects.count()
        url = reverse("panels:server_action", args=[self.srv.pk])
        free = Unit.objects.get(serial_number="C")
        a = Unit.objects.get(serial_number="A")
        self.client.post(url, dict(action="remove", units=[a.pk]))                         # no location
        self.client.post(url, dict(action="remove", units=[free.pk], location=self.main.pk))  # not installed
        self.client.post(url, dict(action="install_stock", units=[a.pk]))                  # not free
        self.client.post(url, dict(action="replace", old=free.pk, new_units=[], location=self.main.pk))
        self.client.post(url, dict(action="install_new", item=self.r64.pk, quantity=0))
        self.client.post(url, dict(action="nope"))
        self.assertEqual(StockDocument.objects.count(), before)

    # ---- edits
    def test_edit_unit_goes_through_approval(self):
        a = Unit.objects.get(serial_number="A")
        r = self.client.post(reverse("panels:unit_edit", args=[a.pk]), dict(serial_number="A2", label="top", note="typo"))
        self.assertEqual(r.status_code, 302)
        a.refresh_from_db(); self.assertEqual(a.serial_number, "A")
        self.assertContains(self.client.get(reverse("panels:unit", args=[a.pk])), "ویرایش در انتظار تأیید")
        self.client.force_login(self.manager)
        page = self.client.get(reverse("panels:approvals"))
        self.assertContains(page, "A2")
        from inventory.models import ChangeRequest
        cr = ChangeRequest.objects.get()
        self.client.post(reverse("panels:change_approve", args=[cr.pk]))
        a.refresh_from_db(); self.assertEqual((a.serial_number, a.label), ("A2", "top"))

    def test_edit_item_capacity_updates_server_totals(self):
        self.client.post(reverse("panels:item_edit", args=[self.r32.pk]), dict(
            name="RAM 32", category=self.r32.category_id, unit_label="عدد", min_stock=0,
            capacity="16", capacity_unit="GB", is_active="on"))
        from inventory.models import ChangeRequest
        self.client.force_login(self.manager)
        self.client.post(reverse("panels:change_approve", args=[ChangeRequest.objects.get().pk]))
        self.assertContains(self.client.get(reverse("panels:unit", args=[self.srv.pk])), "32GB")

    def test_edit_validation_and_reject(self):
        a = Unit.objects.get(serial_number="A")
        r = self.client.post(reverse("panels:unit_edit", args=[a.pk]), dict(serial_number="B", label=""))
        self.assertContains(r, "قبلاً ثبت شده")
        r = self.client.post(reverse("panels:unit_edit", args=[a.pk]), dict(serial_number="A", label=""))
        self.assertContains(r, "هیچ تغییری")
        self.client.post(reverse("panels:unit_edit", args=[a.pk]), dict(serial_number="A9", label=""))
        from inventory.models import ChangeRequest
        cr = ChangeRequest.objects.get()
        self.client.force_login(self.manager)
        self.client.post(reverse("panels:change_reject", args=[cr.pk]), dict(reason="خیر"))
        cr.refresh_from_db(); self.assertEqual(cr.status, "rejected")

    def test_keeper_cannot_decide_changes(self):
        a = Unit.objects.get(serial_number="A")
        self.client.post(reverse("panels:unit_edit", args=[a.pk]), dict(serial_number="A9", label=""))
        from inventory.models import ChangeRequest
        r = self.client.post(reverse("panels:change_approve", args=[ChangeRequest.objects.get().pk]))
        self.assertEqual(r.status_code, 403)

    # ---- misc
    def test_admin_approves_own_request_through_the_panel(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("panels:new_request"), formset([dict(
            item_type=self.d6.pk, operation="purchase", quantity=1, to_location=self.main.pk)], key="mine"))
        doc = StockDocument.objects.get(idempotency_key="mine")
        r = self.client.post(reverse("panels:approve", args=[doc.pk]), follow=True)
        doc.refresh_from_db(); self.assertEqual(doc.status, "approved")

    def test_items_list_has_show_button(self):
        r = self.client.get(reverse("panels:items"))
        self.assertContains(r, ">نمایش</a>")
        self.assertContains(r, reverse("panels:item", args=[self.r32.pk]))

    def test_server_item_page_lists_hardware_summary(self):
        r = self.client.get(reverse("panels:item", args=[self.srv_t.pk]))
        self.assertContains(r, "RAM 64GB")
        self.assertContains(r, "Disk 1.2TB")
