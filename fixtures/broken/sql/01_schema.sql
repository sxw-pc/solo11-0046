-- Synthetic negative fixtures: duplicate definition and a cycle.
CREATE TABLE raw.orders (
  order_id INT PRIMARY KEY,
  gross_amount DECIMAL(12,2) NOT NULL,
  net_amount DECIMAL(12,2),
  currency VARCHAR(3) NOT NULL,
  note VARCHAR(40),
  booked_at TIMESTAMP
);

CREATE TABLE raw.orders (
  order_id INT PRIMARY KEY,
  gross_amount DECIMAL(12,2) NOT NULL,
  net_amount DECIMAL(12,2),
  currency VARCHAR(3) NOT NULL,
  note VARCHAR(40),
  booked_at TIMESTAMP
);

CREATE TABLE mart.revenue (
  day DATE,
  total_amount VARCHAR(20),
  source_note VARCHAR(40),
  order_count INT
);

CREATE TABLE mart.ledger_a (id INT, val INT);
CREATE TABLE mart.ledger_b (id INT, val INT);
