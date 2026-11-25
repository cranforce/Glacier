import csv
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import connector_shopify as shop  # noqa: E402
import glacier_connectors as connectors  # noqa: E402
import glacier_core as core  # noqa: E402
from shopify_api import ShopifyError  # noqa: E402


def scalar(name, non_null=False):
    t = {"kind": "SCALAR", "name": name, "ofType": None}
    return {"kind": "NON_NULL", "name": None, "ofType": t} if non_null else t


def ref(kind, name):
    return {"kind": kind, "name": name, "ofType": None}


def field(name, type_ref, args=()):
    return {"name": name, "type": type_ref, "args": list(args)}


TYPES = {
    "Order": {"kind": "OBJECT", "interfaces": [{"name": "Node"}], "fields": [
        field("id", scalar("ID", True)),
        field("updatedAt", scalar("DateTime", True)),
        field("displayFinancialStatus", ref("ENUM", "OrderDisplayFinancialStatus")),
        field("tags", {"kind": "NON_NULL", "name": None, "ofType": {"kind": "LIST", "name": None,
                                                                     "ofType": scalar("String", True)}}),
        field("totalPriceSet", ref("OBJECT", "MoneyBag")),
        field("customer", ref("OBJECT", "Customer")),
        field("shippingAddress", ref("OBJECT", "MailingAddress")),
        field("lineItems", ref("OBJECT", "LineItemConnection")),
        field("refunds", {"kind": "LIST", "name": None, "ofType": ref("OBJECT", "Refund")}),
        field("metafield", ref("OBJECT", "Metafield"),
              args=[{"name": "key", "defaultValue": None, "type": scalar("String", True)}]),
    ]},
    "MoneyBag": {"kind": "OBJECT", "interfaces": [], "fields": [
        field("shopMoney", ref("OBJECT", "MoneyV2")), field("presentmentMoney", ref("OBJECT", "MoneyV2"))]},
    "MoneyV2": {"kind": "OBJECT", "interfaces": [], "fields": [
        field("amount", scalar("Decimal", True)), field("currencyCode", ref("ENUM", "CurrencyCode"))]},
    "Customer": {"kind": "OBJECT", "interfaces": [{"name": "Node"}], "fields": [field("id", scalar("ID"))]},
    "MailingAddress": {"kind": "OBJECT", "interfaces": [{"name": "Node"}], "fields": [
        field("id", scalar("ID")), field("city", scalar("String"))]},
}


class NamesTest(unittest.TestCase):
    def test_registered(self):
        self.assertIs(connectors.get("Shopify"), shop)

    def test_column_and_table_names(self):
        self.assertEqual(shop.column_name("totalPriceSet.shopMoney.amount"), "TOTAL_PRICE_SET_SHOP_MONEY_AMOUNT")
        self.assertEqual(shop.column_name("order.updatedAt"), "ORDER_UPDATED_AT")
        self.assertEqual(shop.column_name("billingAddress.address1"), "BILLING_ADDRESS_ADDRESS1")
        self.assertEqual(shop.table_name("orders"), "ORDERS")
        self.assertEqual(shop.table_name("ORDERS.LINEITEMS.TAXLINES"), "ORDERS_LINE_ITEMS_TAX_LINES")
        self.assertEqual(shop.table_name("customers.addressesV2"), "CUSTOMERS_ADDRESSES_V2")
        self.assertEqual(shop.table_name("productVariants"), "PRODUCT_VARIANTS")

    def test_unknown_object(self):
        with self.assertRaises(core.ConfigError):
            shop.object_name("orders.nope")

    def test_catalog_shape(self):
        for obj in shop.OBJECTS.values():
            if obj.is_root:
                self.assertTrue(obj.singular and obj.change and obj.search, obj.name)
                continue
            parent = shop.OBJECTS[".".join(obj.path[:-1]).lower()]
            # A connection hangs off a root; a list off a root or a connection
            self.assertTrue(parent.is_root or (parent.connection and not obj.connection), obj.name)
            if not parent.is_root:
                self.assertTrue(parent.singular, parent.name)

    def test_settings(self):
        s = shop.validate_settings({"SHOP_DOMAIN": "https://Odbm-Europe.myshopify.com/"})
        self.assertEqual(s, {"SHOP_DOMAIN": "odbm-europe.myshopify.com", "API_VERSION": "2026-10", "CLIENT_ID": ""})
        for bad in ({"SHOP_DOMAIN": "shop.example.com"},
                    {"SHOP_DOMAIN": "a.myshopify.com", "API_VERSION": "2026-11"}):
            with self.assertRaises(core.ConfigError):
                shop.validate_settings(bad)

    def test_secret(self):
        self.assertEqual(shop.validate_secret(" shpat_abc "), "shpat_abc")
        with self.assertRaises(core.ConfigError):
            shop.validate_secret("two words")


class KeysTest(unittest.TestCase):
    def test_root(self):
        k = shop.keys("orders")
        self.assertEqual((k.key_col, k.change_col, k.deleted_col), ("ID", "UPDATED_AT", "IS_DELETED"))
        self.assertEqual(shop.keys("tenderTransactions").change_col, "PROCESSED_AT")

    def test_children(self):
        self.assertEqual(shop.keys("orders.lineItems").key_col, "ID")
        self.assertEqual(shop.keys("orders.lineItems").change_col, "ORDER_UPDATED_AT")
        self.assertEqual(shop.keys("orders.taxLines").key_col, "ROW_KEY")

    def test_context_fields(self):
        self.assertEqual([f for f, _ in shop.context_fields(shop.source_object("orders.lineItems.taxLines"))],
                         ["order.id", "order.updatedAt", "lineItem.id", "arrayIndex", "rowKey"])
        self.assertEqual([f for f, _ in shop.context_fields(shop.source_object("orders.fulfillments"))],
                         ["order.id", "order.updatedAt", "adminGraphqlApiId"])
        self.assertEqual(shop.context_fields(shop.source_object("orders")), [("adminGraphqlApiId", "String")])
        self.assertEqual(shop.column_name("adminGraphqlApiId"), "ADMIN_GRAPHQL_API_ID")


class FieldsTest(unittest.TestCase):
    def test_type_fields(self):
        fields = shop.type_fields(TYPES.__getitem__, "Order")
        self.assertEqual([f[0] for f in fields], [
            "id", "updatedAt", "displayFinancialStatus", "tags",
            "totalPriceSet.shopMoney.amount", "totalPriceSet.shopMoney.currencyCode",
            "totalPriceSet.presentmentMoney.amount", "totalPriceSet.presentmentMoney.currencyCode",
            "customer.id", "shippingAddress.id", "shippingAddress.city"])
        by_path = {f[0]: f for f in fields}
        self.assertEqual(by_path["tags"], ("tags", "String", True, False))
        # A nullable parent makes its non-null fields nullable
        self.assertTrue(by_path["totalPriceSet.shopMoney.amount"][3])

    def test_field_rows(self):
        obj = shop.source_object("orders.lineItems")
        rows = shop.field_rows(obj, [("id", "ID", False, False), ("quantity", "Int", False, False),
                                     ("totalPriceSet.shopMoney.amount", "Decimal", False, True)],
                               denied={"totalPriceSet"})
        self.assertEqual([r["name"] for r in rows],
                         ["order.id", "order.updatedAt", "adminGraphqlApiId", "id", "quantity",
                          "totalPriceSet.shopMoney.amount", "isDeleted"])
        self.assertEqual([r["snowflakeType"] for r in rows],
                         ["NUMBER(38,0)", "TIMESTAMP_NTZ", "VARCHAR", "NUMBER(38,0)", "NUMBER(38,0)",
                          "NUMBER(38,10)", "BOOLEAN"])
        self.assertEqual([r["exportable"] for r in rows], [True, True, True, True, True, False, True])

    def test_denied_paths(self):
        obj = shop.source_object("orders.lineItems")
        errors = [{"message": "Access denied", "path": ["orders", "edges", 0, "node", "lineItems", "edges", 0,
                                                         "node", "staffMember"]}]
        self.assertEqual(shop.denied_paths(obj, errors), {"staffMember"})
        with self.assertRaises(ShopifyError):
            shop.denied_paths(obj, [{"message": "Parse error"}])


class QueryTest(unittest.TestCase):
    def test_selection(self):
        self.assertEqual(shop.selection(["id", "totalPriceSet.shopMoney.amount", "totalPriceSet.shopMoney.currencyCode"]),
                         "id totalPriceSet { shopMoney { amount currencyCode } }")

    def test_root_query(self):
        q = shop.build_query("orders", ["name"], since="2026-11-20T00:00:00Z", extra_fields=("isDeleted",))
        self.assertEqual(q.split("\n"), [
            "# Glacier object: orders",
            "# Glacier columns: id, updatedAt, adminGraphqlApiId, isDeleted, name",
            "{ orders(query: \"updated_at:>='2026-11-20T00:00:00Z'\") { edges { node { id updatedAt name } } } }"])
        self.assertEqual(shop.query_columns(q), ["ID", "UPDATED_AT", "ADMIN_GRAPHQL_API_ID", "IS_DELETED", "NAME"])

    def test_connection_child(self):
        q = shop.build_query("orders.lineItems", ["quantity"])
        self.assertEqual(q.split("\n")[1], "# Glacier columns: id, order.updatedAt, order.id, adminGraphqlApiId, quantity")
        self.assertEqual(q.split("\n")[2], "{ orders { edges { node { id updatedAt lineItems { edges { node { "
                                           "id quantity } } } } } } }")

    def test_list_children(self):
        self.assertEqual(shop.build_query("orders.taxLines", ["rate"]).split("\n")[2],
                         "{ orders { edges { node { id updatedAt taxLines { rate } } } } }")
        self.assertEqual(shop.build_query("orders.lineItems.taxLines", ["rate"]).split("\n")[2],
                         "{ orders { edges { node { id updatedAt lineItems { edges { node { "
                         "id taxLines { rate } } } } } } } }")

    def test_ids_query(self):
        self.assertEqual(shop.build_ids_query("orders.taxLines").split("\n")[1:],
                         ["# Glacier columns: rowKey", "{ orders { edges { node { id updatedAt taxLines { __typename } } } } }"])

    def test_probe(self):
        obj = shop.source_object("orders.lineItems.taxLines")
        self.assertEqual(shop._graphql(obj, ["rate"], probe=True),
                         "{ orders(first: 1) { edges { node { id updatedAt lineItems(first: 5) { edges { node { "
                         "id taxLines { rate } } } } } } } }")

    def test_bad_watermark(self):
        with self.assertRaises(core.ConfigError):
            shop.build_query("orders", ["name"], since="2026-11-20' OR '1")

    def test_bad_field(self):
        with self.assertRaises(core.ConfigError):
            shop.build_query("orders", ["name }"])


def rows_of(name, columns, lines):
    return list(shop.records(shop.source_object(name), columns, lines))


class RecordsTest(unittest.TestCase):
    ORDER = {"id": "gid://shopify/Order/1", "updatedAt": "2026-11-20T10:00:00Z", "name": "#1001",
             "test": False, "tags": ["vip"], "totalPriceSet": {"shopMoney": {"amount": "12.50"}}, "customer": None,
             "taxLines": [{"rate": 0.08}, {"rate": 0.02}]}

    def test_root_rows(self):
        self.assertEqual(rows_of("orders", ["id", "updatedAt", "adminGraphqlApiId", "isDeleted", "test", "tags",
                                            "totalPriceSet.shopMoney.amount", "customer.id"], [self.ORDER]),
                         [["1", "2026-11-20T10:00:00Z", "gid://shopify/Order/1", "false", "false", '["vip"]',
                           "12.50", None]])

    def test_list_child_rows(self):
        self.assertEqual(rows_of("orders.taxLines", ["rowKey", "order.updatedAt", "order.id", "arrayIndex", "rate",
                                                     "isDeleted"], [self.ORDER]),
                         [["1:0", "2026-11-20T10:00:00Z", "1", "0", "0.08", "false"],
                          ["1:1", "2026-11-20T10:00:00Z", "1", "1", "0.02", "false"]])

    def test_connection_child_waits_for_its_parent(self):
        lines = [
            {"id": "gid://shopify/LineItem/7", "quantity": 2, "__parentId": "gid://shopify/Order/1",
             "taxLines": [{"rate": 0.08}]},
            {"id": "gid://shopify/Order/1", "updatedAt": "2026-11-20T10:00:00Z"},
            {"id": "gid://shopify/Order/2", "updatedAt": "2026-11-21T10:00:00Z"},
            {"id": "gid://shopify/LineItem/8", "quantity": 1, "__parentId": "gid://shopify/Order/2", "taxLines": []},
        ]
        self.assertEqual(rows_of("orders.lineItems", ["id", "order.updatedAt", "order.id", "adminGraphqlApiId",
                                                      "quantity"], lines),
                         [["8", "2026-11-21T10:00:00Z", "2", "gid://shopify/LineItem/8", "1"],
                          ["7", "2026-11-20T10:00:00Z", "1", "gid://shopify/LineItem/7", "2"]])
        self.assertEqual(rows_of("orders.lineItems.taxLines",
                                 ["rowKey", "order.updatedAt", "order.id", "lineItem.id", "rate"], lines),
                         [["7:0", "2026-11-20T10:00:00Z", "1", "7", "0.08"]])
        # Ids need no parent
        self.assertEqual(rows_of("orders.lineItems", ["id"], lines[:1]), [["7"]])

    def test_missing_parent(self):
        with self.assertRaises(ShopifyError):
            rows_of("orders.lineItems", ["id", "order.updatedAt"],
                    [{"id": "gid://shopify/LineItem/7", "__parentId": "gid://shopify/Order/9"}])

    def test_gid_numbers(self):
        self.assertEqual(shop.gid_number("gid://shopify/MailingAddress/5?model_name=CustomerAddress"), "5")
        self.assertEqual(shop.gid_number("gid://shopify/Order/abc"), "gid://shopify/Order/abc")

    def test_csv_pages(self):
        pages = list(shop.csv_pages(iter([["1", None], ["2", 'say "hi"']]), ["id", "note"], page_size=1))
        self.assertEqual([n for _, n in pages], [1, 1])
        self.assertEqual(list(csv.reader(io.StringIO(pages[1][0].decode()))), [["id", "note"], ["2", 'say "hi"']])
        self.assertEqual(list(shop.csv_pages(iter([]), ["id"])), [(b'"id"\n', 0)])


class ExportTest(unittest.TestCase):
    def test_export_runs_the_query_without_its_comments(self):
        sent = []

        class Client:
            def bulk_query(self, graphql):
                sent.append(graphql)
                return iter([{"id": "gid://shopify/Order/1", "updatedAt": "2026-11-20T10:00:00Z", "name": "#1"}])

        original = shop.client
        shop.client = lambda settings, secret: Client()
        try:
            pages = list(shop.export({}, "x", shop.build_query("orders", ["name"]), True))
        finally:
            shop.client = original
        self.assertEqual(sent, ["{ orders { edges { node { id updatedAt name } } } }"])
        self.assertEqual(pages, [(b'"id","updatedAt","adminGraphqlApiId","name"\n'
                                  b'"1","2026-11-20T10:00:00Z","gid://shopify/Order/1","#1"\n', 1)])


if __name__ == "__main__":
    unittest.main()
