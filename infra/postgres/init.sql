-- Schema and seed data for the shop database.
-- Runs once, when the database container is created (docker-entrypoint-initdb.d).

CREATE TABLE products (
    id    SERIAL PRIMARY KEY,
    name  TEXT NOT NULL,
    price DOUBLE PRECISION NOT NULL
);

CREATE TABLE stock (
    product_id INT PRIMARY KEY REFERENCES products(id),
    quantity   INT NOT NULL
);

CREATE TABLE orders (
    id          BIGSERIAL PRIMARY KEY,
    customer_id INT NOT NULL,
    product_id  INT NOT NULL,
    quantity    INT NOT NULL,
    amount      DOUBLE PRECISION NOT NULL,
    charge_id   TEXT,
    status      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Used by the order service's fraud check (recent orders of a customer).
CREATE INDEX orders_customer_created_idx ON orders (customer_id, created_at);

INSERT INTO products (name, price) VALUES
    ('Margherita pizza', 8.50), ('Pepperoni pizza', 9.90), ('Caesar salad', 6.40),
    ('Chicken burger', 7.80), ('Veggie burger', 7.20), ('French fries', 2.90),
    ('Chicken wings', 6.90), ('Sushi set', 14.50), ('Ramen', 10.20), ('Pad thai', 9.60),
    ('Falafel wrap', 6.10), ('Beef plov', 8.90), ('Lagman', 8.30), ('Manti (6 pcs)', 7.40),
    ('Cheesecake', 4.80), ('Tiramisu', 5.20), ('Lemonade', 2.50), ('Green tea', 1.80),
    ('Espresso', 2.20), ('Cola', 1.90);

INSERT INTO stock (product_id, quantity) SELECT id, 1000000 FROM products;

-- 3 million historical orders: a production-sized table, so that a query
-- without its index (scenario s09) is really slow (a full scan takes ~75 ms).
INSERT INTO orders (customer_id, product_id, quantity, amount, charge_id, status, created_at)
SELECT 1 + (random() * 499)::int,
       1 + (random() * 19)::int,
       1 + (random() * 2)::int,
       round((random() * 50)::numeric, 2),
       'ch_seed_' || g,
       'confirmed',
       now() - random() * interval '90 days'
FROM generate_series(1, 3000000) AS g;

ANALYZE;
