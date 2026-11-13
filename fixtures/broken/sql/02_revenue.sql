INSERT INTO mart.revenue (day, total_amount, source_note, order_count)
SELECT CAST(booked_at AS DATE) AS day,
       net_amount AS total_amount,
       note AS source_note,
       COUNT(*) AS order_count
FROM raw.orders
GROUP BY CAST(booked_at AS DATE), net_amount, note;
