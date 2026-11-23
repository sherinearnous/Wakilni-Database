const choices = JSON.parse(document.getElementById("order-options").textContent);
const merchant = document.getElementById("merchant");
const locationField = document.getElementById("location");
const productField = document.getElementById("product");
const price = document.getElementById("price");

function fill(select, records, label, selected) {
  const matching = records.filter((record) => record.merchant === merchant.value);
  select.replaceChildren(new Option(merchant.value ? label : "Choose a business group first", ""));
  for (const record of matching) {
    select.add(new Option(record.label, record.id, false, record.id === selected));
  }
  select.disabled = !matching.length;
}

function showPrice() {
  const product = choices.products.find((record) => record.id === productField.value);
  price.textContent = product ? `Catalog price: $${product.price.toFixed(2)} per unit` : "";
}

function refresh(location = "", product = "") {
  fill(locationField, choices.locations, "Choose a location", location);
  fill(productField, choices.products, "Choose a product", product);
  showPrice();
}

merchant.addEventListener("change", () => refresh());
productField.addEventListener("change", showPrice);
refresh(locationField.dataset.selected, productField.dataset.selected);
