const fleet = JSON.parse(document.getElementById("fleet-options").textContent);
const driver = document.getElementById("driver");
const vehicle = document.getElementById("vehicle");
const note = document.getElementById("fleet-note");

function listVehicles(selected) {
  const row = fleet.find((item) => item.driverID === driver.value);
  vehicle.replaceChildren(new Option(row ? "Choose a compatible vehicle" : "Choose a driver first", ""));
  vehicle.disabled = !row;
  for (const item of row?.vehicles ?? []) {
    vehicle.add(new Option(`${item.type} · ${item.plate} · ${item.id} · License ${item.license}`,
      item.id, false, item.id === selected));
  }
  showTransfer(row);
}

function showTransfer(row) {
  if (!note) return;
  const selected = row?.vehicles.find((item) => item.id === vehicle.value);
  note.textContent = selected?.owner && selected.owner !== driver.value
    ? "This transfers the current vehicle from the previous driver to the selected driver."
    : "";
}

driver.addEventListener("change", () => listVehicles(vehicle.value));
vehicle.addEventListener("change", () => showTransfer(fleet.find((item) => item.driverID === driver.value)));
listVehicles(vehicle.dataset.selected);
