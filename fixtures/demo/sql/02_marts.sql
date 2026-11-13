CREATE TABLE mart.daily_revenue (
  order_date DATE NOT NULL,
  revenue DECIMAL(18,2) NOT NULL,
  order_count BIGINT NOT NULL
);

INSERT INTO mart.daily_revenue (order_date, revenue, order_count)
SELECT CAST(order_ts AS DATE) AS order_date,
       SUM(net_amount) AS revenue,
       COUNT(*) AS order_count
FROM raw.orders
GROUP BY CAST(order_ts AS DATE);

CREATE TABLE mart.customer_revenue (
  customer_id BIGINT NOT NULL,
  region VARCHAR(16) NOT NULL,
  revenue DECIMAL(18,2) NOT NULL
);

INSERT INTO mart.customer_revenue (customer_id, region, revenue)
SELECT o.customer_id,
       c.region,
       SUM(o.net_amount) AS revenue
FROM raw.orders AS o
JOIN raw.customers AS c ON o.customer_id = c.customer_id
GROUP BY o.customer_id, c.region;
