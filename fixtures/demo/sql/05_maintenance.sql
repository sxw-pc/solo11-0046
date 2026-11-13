ALTER TABLE raw.orders ADD COLUMN channel VARCHAR(16);
DROP TABLE IF EXISTS mart.customer_rollup;
