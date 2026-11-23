from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import secrets
import sqlite3

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from dispatch import LICENSES, METHODS, assign, assignment_options, reassign_shipment, shipment_fleet


ROOT = Path(__file__).resolve().parent
DEMO_TIME = datetime(2026, 11, 22, 15)
DATE_FORMAT = "%d/%m/%Y %H:%M"
ORDER_STATES = ("Pending", "Picked Up", "In Transit", "Delivered", "Returned", "Cancelled")
SHIPMENT_STATES = ("Planned", "Dispatched", "In Transit", "Completed", "Cancelled")
PAYMENT_METHODS = ("Prepaid", "Card", "Cash on Delivery")
ORDER_TYPES = ("Delivery", "Return", "Exchange")
VEHICLE_TYPES = tuple(LICENSES)
PAGE_SIZE = 15

app = Flask(__name__)
render_hosted = os.environ.get("RENDER") == "true"
app.config.from_mapping(
    DATABASE=os.environ.get("WAKILNI_DATABASE", ROOT / "wakilni_demo.sqlite"),
    SECRET_KEY=os.environ.get("WAKILNI_SECRET_KEY") or secrets.token_hex(32),
    RENDER_HOSTED=render_hosted,
    DEMO_PASSWORD=os.environ.get("WAKILNI_DEMO_PASSWORD", ""),
    SESSION_COOKIE_SECURE=render_hosted,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=64 * 1024,
)


@app.before_request
def protect_online_demo():
    password = app.config["DEMO_PASSWORD"]
    if app.config["RENDER_HOSTED"] and len(password) < 12:
        return Response("Set a demo password of at least 12 characters before opening the online site.\n", 503)
    if not password or request.endpoint == "health":
        return None

    auth = request.authorization
    if (
        auth and auth.type == "basic"
        and secrets.compare_digest((auth.username or "").encode("utf-8"), b"professor")
        and secrets.compare_digest((auth.password or "").encode("utf-8"), password.encode("utf-8"))
    ):
        return None
    return Response("Use the demo login to open Wakilni.\n", 401,
                    {"WWW-Authenticate": 'Basic realm="Wakilni class demo", charset="UTF-8"',
                     "Cache-Control": "no-store"})


@app.get("/health")
def health():
    try:
        db().execute('SELECT orderID FROM "Order" LIMIT 1').fetchone()
    except sqlite3.Error:
        return Response("Unavailable\n", 503)
    return Response("OK\n", 200, mimetype="text/plain")


def db():
    if "db" not in g:
        path = Path(app.config["DATABASE"]).resolve()
        g.db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


@app.template_filter("money")
def money(value):
    return f"${value:,.2f}"


@app.template_filter("number")
def number(value):
    return f"{value:,}"


def date_key(column):
    # Column names are supplied by application code. Stored dates are day-first.
    return f"substr({column}, 7, 4) || substr({column}, 4, 2) || substr({column}, 1, 2) || substr({column}, 11)"


ORDERS_FROM = """
FROM "Order" o
JOIN "Business Location" l USING (businesslocationID)
JOIN "Business Group" b USING (businessgroupID)
LEFT JOIN Address a ON (a.addressLatitude, a.addressLongitude) = (l.addressLatitude, l.addressLongitude)
LEFT JOIN PostalCode p ON p.addressPostalCode = a.addressPostalCode
"""
ORDERS_FIELDS = "o.*, b.businessgroupName, b.businessgroupID, b.operationalStatusWithWakilni, p.addressCity, a.addressBuilding, a.addressStreet"
SHIPMENTS_FROM = """
FROM Shipment s
JOIN Driver d USING (driverID)
JOIN Vehicle v ON v.vehicleID = s.vehicleID
"""
SHIPMENTS_FIELDS = "s.*, d.driverFirstName || ' ' || d.driverLastName AS driverName, v.vehicleRegistrationNo, v.vehicleType"
PRODUCTS_FROM = """
FROM Product p JOIN "Business Group" b USING (businessgroupID)
LEFT JOIN "Inventory Location" i USING (inventorylocationID)
LEFT JOIN AisleZone z ON z.warehouseID = i.warehouseID AND z.inventorylocationAisle = i.inventorylocationAisle
LEFT JOIN Warehouse w ON w.warehouseID = i.warehouseID
LEFT JOIN PostalCode pc ON pc.addressPostalCode = w.addressPostalCode
LEFT JOIN Packaging k USING (packagingID)
"""
PRODUCTS_FIELDS = """p.*, b.businessgroupName, i.warehouseID, w.warehouseName, pc.addressCity,
                     z.inventorylocationZone, i.inventorylocationAisle, i.inventorylocationRack,
                     i.inventorylocationShelf, i.maxWeight AS binWeight, i.maxVolume AS binVolume,
                     k.packagingType, k.packagingConfiguration, k.unitsPerPackage,
                     k.length, k.width, k.height, k.maxWeight AS packagingMaxWeight, k.packagingWeight"""
DRIVERS_FROM = """
FROM Driver d LEFT JOIN Vehicle v USING (vehicleID)
LEFT JOIN (
    SELECT driverID, group_concat(driverLicenseCategory, ', ') AS licenses
    FROM (SELECT driverID, driverLicenseCategory FROM DriverLicenseCategory ORDER BY driverID, driverLicenseCategory)
    GROUP BY driverID
) dl USING (driverID)
"""
DRIVERS_FIELDS = "d.*, v.vehicleRegistrationNo, v.vehicleType, dl.licenses"
WAREHOUSES_FROM = """
FROM Warehouse w LEFT JOIN PostalCode pc USING (addressPostalCode)
LEFT JOIN City c USING (addressCity) LEFT JOIN District d USING (addressDistrict)
"""
WAREHOUSES_FIELDS = "w.*, pc.addressCity, c.addressDistrict, d.addressCountry"
VEHICLES_FROM = "FROM Vehicle v LEFT JOIN Driver d USING (vehicleID)"
VEHICLES_FIELDS = "v.*, d.driverID, d.driverFirstName || ' ' || d.driverLastName AS driverName"
MOVEMENT_STATES = ("Completed", "In Progress", "Pending", "Cancelled")
MOVEMENT_TYPES = ("Inbound", "Outbound", "Transfer", "Adjustment")
MOVEMENTS_FROM = """
FROM "Inventory Movement" m JOIN "Inventory Location" i USING (inventorylocationID)
JOIN AisleZone z ON z.warehouseID = i.warehouseID AND z.inventorylocationAisle = i.inventorylocationAisle
JOIN Warehouse w ON w.warehouseID = i.warehouseID
"""
MOVEMENTS_FIELDS = """m.*, w.warehouseID, w.warehouseName, z.inventorylocationZone,
                     i.inventorylocationAisle, i.inventorylocationRack, i.inventorylocationShelf"""
RECEIPTS_FROM = """
FROM "Delivery Receipt" r JOIN "Order" o ON o.orderID = r.orderID
JOIN "Business Group" b ON b.businessgroupID = r.businessgroupID
JOIN Shipment s ON s.shipmentID = r.shipmentID
"""
RECEIPTS_FIELDS = """r.*, o.orderTrackingNumber, o.deliveryFee, o.orderPaymentMethod,
                    o.orderType, b.businessgroupName, s.shipmentStatus"""


@app.get("/")
def home():
    conn = db()
    states = dict(conn.execute('SELECT status, count(*) FROM "Order" GROUP BY status'))
    total_orders = sum(states.values())
    open_shipments = conn.execute(
        "SELECT count(*) FROM Shipment WHERE shipmentStatus IN ('Planned', 'Dispatched', 'In Transit')"
    ).fetchone()[0]
    group_count = conn.execute('SELECT count(*) FROM "Business Group"').fetchone()[0]
    latest = conn.execute(
        f"SELECT {ORDERS_FIELDS} {ORDERS_FROM} ORDER BY {date_key('o.orderDateAndTime')} DESC, o.orderID DESC LIMIT 6"
    ).fetchall()
    status_rows = [(state, states.get(state, 0), 100 * states.get(state, 0) / max(total_orders, 1))
                   for state in ("Delivered", "Pending", "Cancelled", "In Transit", "Returned", "Picked Up")]
    return render_template("home.html", latest=latest, states=states, total_orders=total_orders,
                           open_shipments=open_shipments, group_count=group_count, status_rows=status_rows)


def date_filters(column, caption):
    day = f"substr(({date_key(column)}), 1, 8)"
    return (
        ("from_date", f"{caption} from", f"{day} >= ?", None),
        ("to_date", f"{caption} through", f"{day} <= ?", None),
    )


def lookup_choices(sql):
    return [tuple(row) for row in db().execute(sql)]


WAREHOUSE_CHOICES = "SELECT warehouseID, warehouseName FROM Warehouse ORDER BY warehouseName, warehouseID"
GROUP_CHOICES = 'SELECT businessgroupID, businessgroupName FROM "Business Group" ORDER BY businessgroupName, businessgroupID'


def list_page(select, source, fields, ordering, filters, scope=(), search_value=None):
    query = request.args.get("q", "").strip()[:100]
    clauses = [condition for _, condition, _ in scope]
    params = [value for _, _, value in scope]
    page_args = {name: value for name, _, value in scope}
    if query:
        clauses.append("(" + " OR ".join(f"instr(lower({field}), lower(?)) > 0" for field in fields) + ")")
        params.extend([search_value if search_value is not None else query] * len(fields))
        page_args["q"] = query
    controls = []
    for name, label, condition, choices in filters:
        value = request.args.get(name, "")
        options = [(item, item) if isinstance(item, str) else item for item in choices] if choices is not None else None
        if options is None:
            try:
                parsed = date.fromisoformat(value)
                value = parsed.isoformat()
                bound = value.replace("-", "")
            except ValueError:
                value = ""
                bound = ""
        else:
            bound = value if any(value == key for key, _ in options) else ""
            value = bound
        if bound:
            clauses.append(condition)
            params.append(bound)
            page_args[name] = value
        controls.append(dict(name=name, label=label, value=value, options=options))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = db().execute(f"SELECT count(*) {source} {where}", params).fetchone()[0]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(request.args.get("page", 1, type=int), pages))
    rows = db().execute(
        f"SELECT {select} {source} {where} ORDER BY {ordering} LIMIT ? OFFSET ?",
        [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()
    clear_args = {name: value for name, _, value in scope}
    return dict(rows=rows, q=query, filter_controls=controls, page_args=page_args, clear_args=clear_args,
                page=page, pages=pages, total=total, has_filters=bool(page_args.keys() - clear_args.keys()))


@app.get("/orders")
def orders():
    context = list_page(
        ORDERS_FIELDS, ORDERS_FROM,
        ("o.orderTrackingNumber", "o.orderID", "b.businessgroupName", "p.addressCity"),
        f"{date_key('o.orderDateAndTime')} DESC, o.orderID DESC",
        (
            ("status", "Status", "o.status = ?", ORDER_STATES),
            ("order_type", "Order type", "o.orderType = ?", ORDER_TYPES),
            ("payment", "Payment method", "o.orderPaymentMethod = ?", PAYMENT_METHODS),
            ("city", "Destination city", "p.addressCity = ?",
             lookup_choices("SELECT addressCity, addressCity FROM City ORDER BY addressCity")),
            *date_filters("o.orderDateAndTime", "Order date"),
        ),
    )
    return render_template("orders.html", **context)


@app.get("/orders/<order_id>")
def order_detail(order_id):
    order = db().execute(f"SELECT {ORDERS_FIELDS} {ORDERS_FROM} WHERE o.orderID = ?", (order_id,)).fetchone()
    if order is None:
        abort(404)
    items = db().execute(
        """SELECT p.productID, p.productName, p.merchantSKU, op.productQuantity, op.unitPriceAtOrder
           FROM OrderProduct op JOIN Product p USING (productID) WHERE op.orderID = ?
           ORDER BY p.productName, p.productID""", (order_id,)
    ).fetchall()
    allocations = db().execute(
        """SELECT so.shipmentID, so.quantityAllocatedPerProduct, s.shipmentStatus
           FROM ShipmentOrder so JOIN Shipment s USING (shipmentID)
           WHERE so.orderID = ? ORDER BY so.shipmentID""", (order_id,)
    ).fetchall()
    receipt = db().execute('SELECT * FROM "Delivery Receipt" WHERE orderID = ?', (order_id,)).fetchone()
    subtotal = sum(item["productQuantity"] * item["unitPriceAtOrder"] for item in items)
    return render_template("order.html", order=order, items=items, allocations=allocations,
                           receipt=receipt, subtotal=subtotal, can_assign=can_assign_order(order, allocations))


def can_assign_order(order, allocations):
    return (order["status"] == "Pending" and order["orderType"] in ("Delivery", "Exchange")
            and order["operationalStatusWithWakilni"] == "Active" and not allocations)


@app.route("/orders/<order_id>/assign", methods=["GET", "POST"])
def order_assign(order_id):
    conn = db()
    token = session.setdefault("assignment_token", secrets.token_urlsafe(32))
    error = None
    if request.method == "POST":
        supplied = request.form.get("token", "")
        if not supplied.isascii() or not secrets.compare_digest(token, supplied):
            abort(400)

    def get_order():
        record = conn.execute(f"SELECT {ORDERS_FIELDS} {ORDERS_FROM} WHERE o.orderID = ?", (order_id,)).fetchone()
        if record is None:
            abort(404)
        allocations = conn.execute("SELECT 1 FROM ShipmentOrder WHERE orderID = ?", (order_id,)).fetchone()
        if not can_assign_order(record, allocations):
            raise ValueError("Only unallocated pending deliveries or exchanges for an active business group can be assigned.")
        return record

    if request.method == "POST":
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                order = get_order()
                shipment_id = assign(conn, order, request.form, DEMO_TIME, DATE_FORMAT)
        except ValueError as exc:
            error = str(exc)
        except sqlite3.IntegrityError:
            error = "This assignment could not be saved. Choose an available shipment or driver and try again."
        else:
            session.pop("assignment_token", None)
            flash(f"Order assigned to {shipment_id}.")
            return redirect(url_for("order_detail", order_id=order_id), code=303)
    try:
        order = get_order()
        options = assignment_options(conn, order, DEMO_TIME, DATE_FORMAT)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("order_detail", order_id=order_id), code=303)
    default_time = max(options["earliest"], DEMO_TIME + timedelta(days=1))
    return render_template("assign.html", order=order, options=options, token=token, error=error,
                           methods=METHODS, default_time=default_time.isoformat(timespec="minutes"),
                           form=request.form), 400 if error else 200


@app.get("/business-groups")
def business_groups():
    context = list_page(
        """b.*, (SELECT group_concat(t.businessgroupType, ', ') FROM BusinessGroupType t
                 WHERE t.businessgroupID = b.businessgroupID) AS types""",
        'FROM "Business Group" b', ("b.businessgroupName", "b.businessgroupID", "b.businessgroupWebsite"),
        "b.businessgroupName, b.businessgroupID",
        (
            ("status", "Wakilni status", "b.operationalStatusWithWakilni = ?", ("Active", "Inactive", "Suspended")),
            ("payout", "Payout frequency", "b.payoutFrequency = ?", ("Daily", "Weekly", "Bi-Weekly", "Monthly")),
            ("group_type", "Business group type",
             "EXISTS (SELECT 1 FROM BusinessGroupType t WHERE t.businessgroupID = b.businessgroupID AND t.businessgroupType = ?)",
             ("Corporate", "E-commerce", "Pharmacy", "Restaurant", "Retail")),
        ),
    )
    return render_template("groups.html", **context)


@app.get("/business-groups/<group_id>")
def business_group_detail(group_id):
    conn = db()
    group = conn.execute('SELECT * FROM "Business Group" WHERE businessgroupID = ?', (group_id,)).fetchone()
    if group is None:
        abort(404)
    types = conn.execute("SELECT businessgroupType FROM BusinessGroupType WHERE businessgroupID = ? ORDER BY 1", (group_id,)).fetchall()
    phones = conn.execute("SELECT phoneNo FROM BusinessGroupPhoneNo WHERE businessgroupID = ? ORDER BY 1", (group_id,)).fetchall()
    emails = conn.execute("SELECT contactEmails FROM BusinessGroupContactEmails WHERE businessgroupID = ? ORDER BY 1", (group_id,)).fetchall()
    products = conn.execute(
        f"SELECT {PRODUCTS_FIELDS} {PRODUCTS_FROM} WHERE p.businessgroupID = ? ORDER BY p.productName, p.productID",
        (group_id,),
    ).fetchall()
    return render_template("group.html", group=group, types=types, phones=phones, emails=emails, products=products)


@app.get("/products")
def products():
    context = list_page(
        PRODUCTS_FIELDS, PRODUCTS_FROM,
        ("p.productName", "p.productID", "p.merchantSKU", "b.businessgroupName", "w.warehouseName", "i.warehouseID", "z.inventorylocationZone"),
        "p.productName, p.productID",
        (
            ("warehouse", "Warehouse", "w.warehouseID = ?", lookup_choices(WAREHOUSE_CHOICES)),
            ("status", "Zone", "z.inventorylocationZone = ?", ("Ambient", "Cold", "Bulk", "Picking")),
            ("group", "Business group", "b.businessgroupID = ?", lookup_choices(GROUP_CHOICES)),
            ("packaging", "Packaging type", "k.packagingType = ?", ("Bag", "Box", "Crate", "Envelope", "Pallet")),
        ),
    )
    return render_template("products.html", **context)


@app.get("/products/<product_id>")
def product_detail(product_id):
    product = db().execute(
        f"SELECT {PRODUCTS_FIELDS} {PRODUCTS_FROM} WHERE p.productID = ?", (product_id,)
    ).fetchone()
    if product is None:
        abort(404)
    return render_template("product.html", product=product)


@app.get("/drivers")
def drivers():
    context = list_page(
        DRIVERS_FIELDS, DRIVERS_FROM,
        ("d.driverID", "d.driverFirstName || ' ' || d.driverLastName", "d.driverMiddleName", "v.vehicleRegistrationNo"),
        "d.driverFirstName, d.driverLastName, d.driverID",
        (
            ("status", "Employment status", "d.employmentStatus = ?",
             (("active", "Active"), ("pending", "Pending"), ("suspended", "Suspended"), ("terminated", "Terminated"))),
            ("license", "License category",
             "EXISTS (SELECT 1 FROM DriverLicenseCategory c WHERE c.driverID = d.driverID AND c.driverLicenseCategory = ?)",
             ("A", "B", "C", "D")),
            ("vehicle_type", "Assigned vehicle type", "v.vehicleType = ?", VEHICLE_TYPES),
        ),
    )
    return render_template("drivers.html", **context)


@app.get("/drivers/<driver_id>")
def driver_detail(driver_id):
    conn = db()
    driver = conn.execute(
        f"""SELECT {DRIVERS_FIELDS}, ot.overtimeFee {DRIVERS_FROM}
            LEFT JOIN OvertimeFee ot ON ot.driverSalary = d.driverSalary AND ot.driverOvertimeHours = d.driverOvertimeHours
            WHERE d.driverID = ?""", (driver_id,)
    ).fetchone()
    if driver is None:
        abort(404)
    phones = conn.execute("SELECT phoneNo FROM DriverPhoneNo WHERE driverID = ? ORDER BY 1", (driver_id,)).fetchall()
    emails = conn.execute("SELECT driverEmail FROM DriverEmail WHERE driverID = ? ORDER BY 1", (driver_id,)).fetchall()
    categories = [
        (category, [vehicle for vehicle, required in LICENSES.items() if required == category])
        for category in (driver["licenses"] or "").split(", ") if category
    ]
    routes = conn.execute(
        f"""SELECT {SHIPMENTS_FIELDS} {SHIPMENTS_FROM} WHERE s.driverID = ?
            ORDER BY {date_key('s.plannedDispatchDateAndTime')} DESC, s.shipmentID DESC""", (driver_id,)
    ).fetchall()
    return render_template("driver.html", driver=driver, categories=categories, phones=phones, emails=emails, routes=routes)


@app.get("/warehouses")
def warehouses():
    context = list_page(
        WAREHOUSES_FIELDS, WAREHOUSES_FROM, ("w.warehouseName", "w.warehouseID", "pc.addressCity"),
        "w.warehouseName, w.warehouseID",
        (
            ("status", "Capacity status", "w.capacityStatus = ?", ("Available", "Near Full", "Full")),
            ("operational", "Operational status", "w.operationalStatus = ?", ("Operational", "Maintenance", "Closed")),
            ("warehouse_type", "Warehouse type", "w.warehouseType = ?", ("Cold Storage", "Cross-Dock", "Distribution", "Fulfillment")),
            ("city", "City", "pc.addressCity = ?", lookup_choices(
                "SELECT DISTINCT pc.addressCity, pc.addressCity FROM Warehouse w JOIN PostalCode pc USING (addressPostalCode) ORDER BY 1")),
        ),
    )
    return render_template("warehouses.html", **context)


@app.get("/warehouses/<warehouse_id>")
def warehouse_detail(warehouse_id):
    conn = db()
    warehouse = conn.execute(f"SELECT {WAREHOUSES_FIELDS} {WAREHOUSES_FROM} WHERE w.warehouseID = ?", (warehouse_id,)).fetchone()
    if warehouse is None:
        abort(404)
    aisles = conn.execute(
        """SELECT z.inventorylocationAisle, z.inventorylocationZone, count(i.inventorylocationID) AS locations
           FROM AisleZone z LEFT JOIN "Inventory Location" i USING (warehouseID, inventorylocationAisle)
           WHERE z.warehouseID = ? GROUP BY z.inventorylocationAisle, z.inventorylocationZone ORDER BY z.inventorylocationAisle""",
        (warehouse_id,),
    ).fetchall()
    rentals = conn.execute(
        """SELECT b.businessgroupID, b.businessgroupName, bgw.spaceRented
           FROM BusinessGroupWarehouse bgw JOIN "Business Group" b USING (businessgroupID)
           WHERE bgw.warehouseID = ? ORDER BY b.businessgroupName, b.businessgroupID""", (warehouse_id,),
    ).fetchall()
    return render_template("warehouse.html", warehouse=warehouse, aisles=aisles, rentals=rentals)


@app.get("/vehicles")
def vehicles():
    context = list_page(
        VEHICLES_FIELDS, VEHICLES_FROM, ("v.vehicleID", "v.vehicleType", "v.vehicleRegistrationNo"),
        "v.vehicleID",
        (
            ("status", "Operational status", "v.vehicleOperationalStatus = ?", ("Active", "In Maintenance", "Out of Service")),
            ("vehicle_type", "Vehicle type", "v.vehicleType = ?", VEHICLE_TYPES),
            ("driver_assignment", "Driver assignment", "CASE WHEN d.driverID IS NULL THEN 'unassigned' ELSE 'assigned' END = ?",
             (("assigned", "Assigned"), ("unassigned", "Unassigned"))),
        ),
    )
    return render_template("vehicles.html", **context)


@app.get("/vehicles/<vehicle_id>")
def vehicle_detail(vehicle_id):
    conn = db()
    vehicle = conn.execute(f"SELECT {VEHICLES_FIELDS} {VEHICLES_FROM} WHERE v.vehicleID = ?", (vehicle_id,)).fetchone()
    if vehicle is None:
        abort(404)
    shipment = conn.execute(f"SELECT {SHIPMENTS_FIELDS} {SHIPMENTS_FROM} WHERE s.vehicleID = ?", (vehicle_id,)).fetchone()
    return render_template("vehicle.html", vehicle=vehicle, shipment=shipment, license=LICENSES.get(vehicle["vehicleType"]))


@app.get("/inventory-movements")
def movements():
    conn = db()
    product_id = request.args.get("product", "")[:8]
    product = conn.execute("SELECT productID, productName FROM Product WHERE productID = ?", (product_id,)).fetchone() if product_id else None
    if product_id and product is None:
        abort(404)
    scope = (("product", "EXISTS (SELECT 1 FROM ProductInventoryMovement pm WHERE pm.movementNo = m.movementNo AND pm.productID = ?)", product_id),) if product else ()
    product_search = """(SELECT group_concat(p.productID || ' ' || p.productName || ' ' || p.merchantSKU, ' ')
                         FROM ProductInventoryMovement pm JOIN Product p USING (productID) WHERE pm.movementNo = m.movementNo)"""
    search = request.args.get("q", "").strip()
    context = list_page(
        MOVEMENTS_FIELDS, MOVEMENTS_FROM,
        ("CAST(m.movementNo AS TEXT)", "m.inventorylocationID", "w.warehouseID", "w.warehouseName", "m.dateAndTime", product_search),
        f"{date_key('m.dateAndTime')} DESC, m.movementNo DESC",
        (
            ("status", "Status", "m.inventorymovementStatus = ?", MOVEMENT_STATES),
            ("kind", "Movement type", "m.inventorymovementType = ?", MOVEMENT_TYPES),
            ("warehouse", "Warehouse", "w.warehouseID = ?", lookup_choices(WAREHOUSE_CHOICES)),
            ("equipment", "Equipment", "m.inventorymovementEquipment = ?", ("Conveyor", "Forklift", "Manual", "Pallet Jack")),
            *date_filters("m.dateAndTime", "Movement date"),
        ),
        scope=scope, search_value=search[1:] if re.fullmatch(r"#[0-9]{1,4}", search) else None,
    )
    return render_template("movements.html", **context, product=product)


@app.get("/inventory-movements/<int:movement_no>")
def movement_detail(movement_no):
    if not 1 <= movement_no <= 9999:
        abort(404)
    conn = db()
    movement = conn.execute(
        f"SELECT {MOVEMENTS_FIELDS} {MOVEMENTS_FROM} WHERE m.movementNo = ?", (movement_no,)
    ).fetchone()
    if movement is None:
        abort(404)
    items = conn.execute(
        """SELECT p.productID, p.productName, p.merchantSKU, pm.quantityPerProductType
           FROM ProductInventoryMovement pm JOIN Product p USING (productID)
           WHERE pm.movementNo = ? ORDER BY p.productName, p.productID""", (movement_no,)
    ).fetchall()
    return render_template("movement.html", movement=movement, items=items)


@app.get("/delivery-receipts")
def receipts():
    context = list_page(
        RECEIPTS_FIELDS, RECEIPTS_FROM,
        ("r.invoiceID", "r.orderID", "o.orderTrackingNumber", "r.shipmentID", "b.businessgroupName", "r.receiptDateAndTime"),
        f"{date_key('r.receiptDateAndTime')} DESC, r.invoiceID DESC",
        (
            ("group", "Business group", "b.businessgroupID = ?", lookup_choices(GROUP_CHOICES)),
            ("order_type", "Order type", "o.orderType = ?", ORDER_TYPES),
            ("payment", "Payment method", "o.orderPaymentMethod = ?", PAYMENT_METHODS),
            *date_filters("r.receiptDateAndTime", "Receipt date"),
        ),
    )
    return render_template("receipts.html", **context)


@app.get("/delivery-receipts/<invoice_id>")
def receipt_detail(invoice_id):
    receipt = db().execute(f"SELECT {RECEIPTS_FIELDS} {RECEIPTS_FROM} WHERE r.invoiceID = ?", (invoice_id,)).fetchone()
    if receipt is None:
        abort(404)
    return render_template("receipt.html", receipt=receipt)


@app.get("/shipments")
def shipments():
    context = list_page(
        SHIPMENTS_FIELDS, SHIPMENTS_FROM,
        ("s.shipmentID", "s.driverID", "d.driverFirstName || ' ' || d.driverLastName", "v.vehicleRegistrationNo"),
        "CASE WHEN s.shipmentStatus IN ('Planned', 'Dispatched', 'In Transit') THEN 0 ELSE 1 END, "
        + date_key("s.plannedDispatchDateAndTime") + " DESC, s.shipmentID DESC",
        (
            ("status", "Status", "s.shipmentStatus = ?", SHIPMENT_STATES),
            ("method", "Shipment method", "s.shipmentMethod = ?", METHODS),
            ("vehicle_type", "Vehicle type", "v.vehicleType = ?", VEHICLE_TYPES),
            *date_filters("s.plannedDispatchDateAndTime", "Dispatch date"),
        ),
    )
    return render_template("shipments.html", **context)


@app.get("/shipments/<shipment_id>")
def shipment_detail(shipment_id):
    shipment = db().execute(
        f"SELECT {SHIPMENTS_FIELDS} {SHIPMENTS_FROM} WHERE s.shipmentID = ?", (shipment_id,)
    ).fetchone()
    if shipment is None:
        abort(404)
    stops = db().execute(
        """SELECT ss.*, a.addressBuilding, a.addressStreet, pc.addressCity
           FROM "Shipment Stop" ss JOIN "Business Location" l USING (businesslocationID)
           LEFT JOIN Address a ON (a.addressLatitude, a.addressLongitude) = (l.addressLatitude, l.addressLongitude)
           LEFT JOIN PostalCode pc ON pc.addressPostalCode = a.addressPostalCode
           WHERE ss.shipmentID = ? ORDER BY ss.shipmentStopNo""", (shipment_id,)
    ).fetchall()
    manifest = db().execute(
        """SELECT o.orderID, o.orderTrackingNumber, so.quantityAllocatedPerProduct, o.status
           FROM ShipmentOrder so JOIN "Order" o USING (orderID) WHERE so.shipmentID = ?
           ORDER BY o.orderID""", (shipment_id,)
    ).fetchall()
    receipts = db().execute(
        """SELECT r.invoiceID, r.receiptDateAndTime, o.orderTrackingNumber
           FROM "Delivery Receipt" r JOIN "Order" o USING (orderID)
           WHERE r.shipmentID = ? ORDER BY r.invoiceID""", (shipment_id,)
    ).fetchall()
    return render_template("shipment.html", shipment=shipment, stops=stops, manifest=manifest, receipts=receipts)


@app.route("/shipments/<shipment_id>/crew", methods=["GET", "POST"])
def shipment_crew(shipment_id):
    conn = db()
    token = session.setdefault("crew_token", secrets.token_urlsafe(32))
    error = None

    def get_planned():
        record = conn.execute(f"SELECT {SHIPMENTS_FIELDS} {SHIPMENTS_FROM} WHERE s.shipmentID = ?", (shipment_id,)).fetchone()
        if record is None:
            abort(404)
        if record["shipmentStatus"] != "Planned":
            raise ValueError("Driver and vehicle changes are available for planned shipments.")
        return record

    if request.method == "POST":
        supplied = request.form.get("token", "")
        if not supplied.isascii() or not secrets.compare_digest(token, supplied):
            abort(400)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                reassign_shipment(conn, get_planned(), request.form)
        except ValueError as exc:
            error = str(exc)
        except sqlite3.IntegrityError:
            error = "This pair could not be saved. Choose an available driver and vehicle."
        else:
            session.pop("crew_token", None)
            flash("Driver and vehicle assignment saved.")
            return redirect(url_for("shipment_detail", shipment_id=shipment_id), code=303)
    try:
        shipment = get_planned()
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("shipment_detail", shipment_id=shipment_id), code=303)
    return render_template("crew.html", shipment=shipment, fleet=shipment_fleet(conn, shipment),
                           token=token, error=error, form=request.form), 400 if error else 200


def form_choices():
    conn = db()
    merchants = conn.execute(
        """SELECT businessgroupID AS id, businessgroupName AS label FROM "Business Group"
           WHERE operationalStatusWithWakilni = 'Active'
             AND EXISTS (SELECT 1 FROM "Business Location" l WHERE l.businessgroupID = "Business Group".businessgroupID)
             AND EXISTS (SELECT 1 FROM Product p WHERE p.businessgroupID = "Business Group".businessgroupID)
           ORDER BY businessgroupName"""
    ).fetchall()
    locations = conn.execute(
        """SELECT l.businesslocationID AS id, l.businessgroupID AS merchant,
                  l.businesslocationType || ' · ' || coalesce(pc.addressCity, 'City unavailable')
                    || ' · ' || l.businesslocationID AS label
           FROM "Business Location" l
           JOIN Address a ON (a.addressLatitude, a.addressLongitude) = (l.addressLatitude, l.addressLongitude)
           JOIN PostalCode pc ON pc.addressPostalCode = a.addressPostalCode
           ORDER BY pc.addressCity, l.businesslocationType, l.businesslocationID"""
    ).fetchall()
    products = conn.execute(
        """SELECT productID AS id, businessgroupID AS merchant, merchantSKU || ' · ' || productName AS label,
                  productUnitPrice AS price FROM Product ORDER BY productName, productID"""
    ).fetchall()
    return dict(merchants=merchants, locations=[dict(row) for row in locations], products=[dict(row) for row in products])


def parse_order(form):
    quantity_text = form.get("quantity", "")
    if not re.fullmatch(r"[0-9]{1,5}", quantity_text) or int(quantity_text) < 1:
        raise ValueError("Enter a whole quantity from 1 to 99,999.")
    try:
        fee = Decimal(form.get("fee", ""))
        if not fee.is_finite() or not 0 <= fee <= Decimal("999.99") or fee != fee.quantize(Decimal(".01")):
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a delivery fee from $0.00 to $999.99 with up to two decimal places.") from None
    method = form.get("payment", "")
    if method not in PAYMENT_METHODS:
        raise ValueError("Choose a payment method.")
    notes = form.get("notes", "").strip()
    if len(notes) > 255:
        raise ValueError("Keep the order notes to 255 characters.")
    requested = form.get("requested", "")
    if requested:
        try:
            planned = datetime.strptime(requested, "%Y-%m-%dT%H:%M")
        except ValueError:
            raise ValueError("Enter a valid requested delivery date and time.") from None
        if planned < DEMO_TIME:
            raise ValueError("Choose a delivery time on or after 22 November 2026 at 15:00.")
        requested = planned.strftime(DATE_FORMAT)
    return int(quantity_text), float(fee), method, notes or None, requested or None


def insert_order(form):
    quantity, fee, method, notes, requested = parse_order(form)
    conn = db()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        product = conn.execute(
            """SELECT p.productID, p.productUnitPrice
               FROM Product p JOIN "Business Group" b USING (businessgroupID)
               JOIN "Business Location" l USING (businessgroupID)
               JOIN Address a ON (a.addressLatitude, a.addressLongitude) = (l.addressLatitude, l.addressLongitude)
               JOIN PostalCode pc ON pc.addressPostalCode = a.addressPostalCode
               WHERE p.productID = ? AND l.businesslocationID = ? AND b.businessgroupID = ?
                 AND b.operationalStatusWithWakilni = 'Active'""",
            (form.get("product"), form.get("location"), form.get("merchant")),
        ).fetchone()
        if product is None:
            raise ValueError("Choose an active business group and a receiving location and product belonging to it.")
        sequence = conn.execute(
            """SELECT max(coalesce(max(CAST(substr(orderID, 3) AS INTEGER)), 0),
                          coalesce(max(CAST(substr(orderTrackingNumber, 3) AS INTEGER)), 0)) + 1 FROM "Order" """
        ).fetchone()[0]
        if sequence > 999999:
            raise ValueError("The demo has reached its order ID limit.")
        order_id, tracking = f"OR{sequence:06d}", f"WK{sequence:010d}"
        conn.execute(
            """INSERT INTO "Order" (orderID, orderDescription, orderCreationSite, orderTrackingNumber,
                                     orderDateAndTime, requestedDeliveryDateAndTime, deliveryFee, status,
                                     orderPaymentMethod, orderType, businesslocationID)
               VALUES (?, ?, 'Website', ?, ?, ?, ?, 'Pending', ?, 'Delivery', ?)""",
            (order_id, notes, tracking, DEMO_TIME.strftime(DATE_FORMAT), requested, fee, method, form["location"]),
        )
        conn.execute(
            "INSERT INTO OrderProduct (orderID, productID, productQuantity, unitPriceAtOrder) VALUES (?, ?, ?, ?)",
            (order_id, product["productID"], quantity, product["productUnitPrice"]),
        )
    return order_id


@app.route("/orders/new", methods=["GET", "POST"])
def order_new():
    token = session.setdefault("order_token", secrets.token_urlsafe(32))
    error = None
    if request.method == "POST":
        supplied = request.form.get("token", "")
        if not supplied.isascii() or not secrets.compare_digest(token, supplied):
            abort(400)
        try:
            order_id = insert_order(request.form)
        except ValueError as exc:
            error = str(exc)
        except sqlite3.IntegrityError:
            error = "SQLite rejected the order. Review the form and try again."
        else:
            session.pop("order_token", None)
            flash("Order saved to SQLite.")
            return redirect(url_for("order_detail", order_id=order_id), code=303)
    return render_template(
        "new.html", token=token, form=request.form, error=error, payments=PAYMENT_METHODS,
        demo_min=DEMO_TIME.isoformat(timespec="minutes"), **form_choices(),
    ), 400 if error else 200


@app.errorhandler(404)
def missing(_error):
    return render_template("missing.html"), 404


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
