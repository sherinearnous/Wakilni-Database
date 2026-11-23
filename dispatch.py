from datetime import datetime, timedelta


METHODS = ("Scheduled", "Standard", "Express", "Same-Day")
LICENSES = {"Motorcycle": "A", "Car": "B", "Van": "B", "Truck": "C"}
SHIPMENT_LOADS = """
WITH line_counts AS (
    SELECT orderID, count(*) AS sku_count FROM OrderProduct GROUP BY orderID
), shipment_lines AS (
    SELECT so.shipmentID,
           CASE WHEN lc.sku_count = 1 THEN so.quantityAllocatedPerProduct ELSE op.productQuantity END AS qty,
           p.unitWeight, p.unitVolume
    FROM ShipmentOrder so JOIN line_counts lc USING (orderID)
    JOIN OrderProduct op USING (orderID) JOIN Product p USING (productID)
), shipment_loads AS (
    SELECT shipmentID, sum(qty) AS units, sum(qty * (unitWeight + .55)) AS kg,
           sum(qty * coalesce(unitVolume, .0025) * 1.22) AS m3
    FROM shipment_lines GROUP BY shipmentID
)
"""


def payload_fits(load, vehicle):
    return (load["units"] <= 9999 and load["kg"] <= vehicle["maxVehicleWeight"] + .000001
            and load["m3"] <= vehicle["maxVehicleCapacity"] + .000001)


def fleet_options(conn, load, shipment=None):
    shipment_id = shipment["shipmentID"] if shipment else ""
    drivers = conn.execute(
        """SELECT d.driverID, d.driverFirstName || ' ' || d.driverLastName AS driverName,
                  d.driveJoinDate, d.vehicleID, group_concat(dl.driverLicenseCategory, ', ') AS licenses
           FROM Driver d JOIN DriverLicenseCategory dl USING (driverID)
           WHERE d.employmentStatus = 'active' AND d.driverLeaveDate IS NULL
             AND NOT EXISTS (SELECT 1 FROM Shipment s WHERE s.driverID = d.driverID AND s.shipmentID <> ?)
           GROUP BY d.driverID ORDER BY driverName, d.driverID""",
        (shipment_id,),
    ).fetchall()
    vehicles = conn.execute(
        """SELECT v.*, d.driverID AS owner
           FROM Vehicle v LEFT JOIN Driver d USING (vehicleID)
           WHERE v.vehicleOperationalStatus = 'Active'
             AND NOT EXISTS (SELECT 1 FROM Shipment s WHERE s.vehicleID = v.vehicleID AND s.shipmentID <> ?)
           ORDER BY v.vehicleType, v.vehicleID""",
        (shipment_id,),
    ).fetchall()
    vehicles = [row for row in vehicles if payload_fits(load, row)]
    can_transfer = shipment and any(row["driverID"] == shipment["driverID"] for row in drivers)
    departure = datetime.strptime(shipment["plannedDispatchDateAndTime"], "%d/%m/%Y %H:%M").date() if shipment else None
    fleet = []
    for driver in drivers:
        joined = datetime.strptime(driver["driveJoinDate"], "%d/%m/%Y").date()
        if departure and joined > departure:
            continue
        licenses = driver["licenses"].split(", ")
        matches = []
        for vehicle in vehicles:
            transferable = can_transfer and vehicle["vehicleID"] == shipment["vehicleID"] and vehicle["owner"] == shipment["driverID"]
            if LICENSES[vehicle["vehicleType"]] in licenses and (vehicle["owner"] in (None, driver["driverID"]) or transferable):
                matches.append(dict(id=vehicle["vehicleID"], type=vehicle["vehicleType"], plate=vehicle["vehicleRegistrationNo"],
                                    license=LICENSES[vehicle["vehicleType"]], owner=vehicle["owner"]))
        if matches:
            matches.sort(key=lambda vehicle: vehicle["id"] != driver["vehicleID"])
            fleet.append(dict(driver, vehicles=matches, earliest=joined.isoformat()))
    return fleet


def selected_pair(fleet, form):
    driver = next((row for row in fleet if row["driverID"] == form.get("driver")), None)
    vehicle = next((row for row in driver["vehicles"] if row["id"] == form.get("vehicle")), None) if driver else None
    if vehicle is None:
        raise ValueError("Choose a compatible, available driver and vehicle from the list.")
    return driver, vehicle


def save_pair(conn, driver, vehicle):
    if vehicle["owner"] and vehicle["owner"] != driver["driverID"]:
        conn.execute("UPDATE Driver SET vehicleID = NULL WHERE driverID = ?", (vehicle["owner"],))
    conn.execute("UPDATE Driver SET vehicleID = ? WHERE driverID = ?", (vehicle["id"], driver["driverID"]))


def shipment_fleet(conn, shipment):
    load = conn.execute(SHIPMENT_LOADS + "SELECT units, kg, m3 FROM shipment_loads WHERE shipmentID = ?",
                        (shipment["shipmentID"],)).fetchone()
    return fleet_options(conn, load, shipment) if load is not None else []


def reassign_shipment(conn, shipment, form):
    driver, vehicle = selected_pair(shipment_fleet(conn, shipment), form)
    save_pair(conn, driver, vehicle)
    conn.execute("UPDATE Shipment SET driverID = ?, vehicleID = ? WHERE shipmentID = ?",
                 (driver["driverID"], vehicle["id"], shipment["shipmentID"]))


def assignment_options(conn, order, snapshot, date_format):
    load = conn.execute(
        """SELECT coalesce(sum(op.productQuantity), 0) AS units,
                  sum(op.productQuantity * (p.unitWeight + .55)) AS kg,
                  sum(op.productQuantity * coalesce(p.unitVolume, .0025) * 1.22) AS m3
           FROM OrderProduct op JOIN Product p USING (productID) WHERE op.orderID = ?""",
        (order["orderID"],),
    ).fetchone()
    if not 1 <= load["units"] <= 9999:
        raise ValueError("This order needs between 1 and 9,999 units to fit on one shipment.")
    pickups = conn.execute(
        """SELECT l.businesslocationID, l.businesslocationType, coalesce(a.addressBuilding, a.addressStreet) AS label, pc.addressCity
           FROM "Business Location" l JOIN Address a USING (addressLatitude, addressLongitude)
           JOIN PostalCode pc USING (addressPostalCode)
           WHERE l.businessgroupID = ? AND l.businesslocationID <> ?
           ORDER BY l.businesslocationType <> 'Warehouse', pc.addressCity, l.businesslocationID""",
        (order["businessgroupID"], order["businesslocationID"]),
    ).fetchall()
    routes = conn.execute(
        SHIPMENT_LOADS + """SELECT s.shipmentID, s.plannedDispatchDateAndTime, s.shipmentMethod, sl.units, sl.kg, sl.m3,
                  v.maxVehicleWeight, v.maxVehicleCapacity
           FROM Shipment s JOIN Vehicle v USING (vehicleID) JOIN shipment_loads sl USING (shipmentID)
           WHERE s.shipmentStatus = 'Planned'
             AND EXISTS (
                 SELECT 1 FROM "Shipment Stop" ss WHERE ss.shipmentID = s.shipmentID
                   AND ss.shipmentstopActionType = 'Delivery' AND ss.businesslocationID = ?
             )
             AND NOT EXISTS (
                 SELECT 1 FROM "Shipment Stop" ss JOIN "Business Location" l USING (businesslocationID)
                 WHERE ss.shipmentID = s.shipmentID AND l.businessgroupID <> ?
             )""",
        (order["businesslocationID"], order["businessgroupID"]),
    ).fetchall()
    created = datetime.strptime(order["orderDateAndTime"], date_format)
    compatible = []
    for route in routes:
        departure = datetime.strptime(route["plannedDispatchDateAndTime"], date_format)
        combined = {key: route[key] + load[key] for key in ("units", "kg", "m3")}
        if departure > snapshot and departure >= created and payload_fits(combined, route):
            compatible.append((departure, route))
    compatible.sort(key=lambda pair: (pair[0], pair[1]["shipmentID"]))
    return dict(units=load["units"], pickups=pickups, fleet=fleet_options(conn, load),
                routes=[route for _, route in compatible], earliest=max(created, snapshot + timedelta(minutes=1)))


def assign(conn, order, form, snapshot, date_format):
    options = assignment_options(conn, order, snapshot, date_format)
    mode = form.get("mode")
    if mode == "existing":
        shipment_id = form.get("shipment")
        if not any(row["shipmentID"] == shipment_id for row in options["routes"]):
            raise ValueError("Choose a compatible planned shipment from the list.")
    elif mode == "new":
        shipment_id = create_shipment(conn, order, form, options, date_format)
    else:
        raise ValueError("Choose how to assign this order.")
    conn.execute(
        "INSERT INTO ShipmentOrder (shipmentID, orderID, quantityAllocatedPerProduct) VALUES (?, ?, ?)",
        (shipment_id, order["orderID"], options["units"]),
    )
    return shipment_id


def create_shipment(conn, order, form, options, date_format):
    driver, vehicle = selected_pair(options["fleet"], form)
    pickup = form.get("pickup")
    method = form.get("method")
    if not any(row["businesslocationID"] == pickup for row in options["pickups"]) or method not in METHODS:
        raise ValueError("Choose a business-group pickup location and a shipment method.")
    try:
        departure = datetime.strptime(form.get("departure", ""), "%Y-%m-%dT%H:%M")
        stop_times = [departure + timedelta(minutes=offset) for offset in (15, 25, 65, 75)]
    except (ValueError, OverflowError):
        raise ValueError("Enter a valid planned dispatch date and time.") from None
    if departure < options["earliest"] or departure.date() < datetime.strptime(driver["driveJoinDate"], "%d/%m/%Y").date():
        raise ValueError("Choose a dispatch time after the demo snapshot and on or after the order creation and driver start dates.")
    sequence = conn.execute("SELECT coalesce(max(CAST(substr(shipmentID, 3) AS INTEGER)), 0) + 1 FROM Shipment").fetchone()[0]
    if sequence > 999999:
        raise ValueError("The demo has reached its shipment ID limit.")
    shipment_id = f"SH{sequence:06d}"
    save_pair(conn, driver, vehicle)
    conn.execute(
        """INSERT INTO Shipment (shipmentID, plannedDispatchDateAndTime, shipmentMethod, shipmentStatus, driverID, vehicleID)
           VALUES (?, ?, ?, 'Planned', ?, ?)""",
        (shipment_id, departure.strftime(date_format), method, driver["driverID"], vehicle["id"]),
    )
    for stop_no, (location, action) in enumerate(((pickup, "Pickup"), (order["businesslocationID"], "Delivery")), 1):
        arrival, leave = stop_times[2 * (stop_no - 1):2 * stop_no]
        conn.execute(
            """INSERT INTO "Shipment Stop" (shipmentID, shipmentStopNo, expectedStopSequence, plannedStopArrival,
                                              plannedLeaveArrival, shipmentstopStatus, shipmentstopActionType,
                                              handlingQuantity, businesslocationID)
               VALUES (?, ?, ?, ?, ?, 'Pending', ?, 0, ?)""",
            (shipment_id, stop_no, str(stop_no), arrival.strftime(date_format), leave.strftime(date_format), action, location),
        )
    return shipment_id
