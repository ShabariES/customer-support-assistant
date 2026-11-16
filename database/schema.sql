-- Form & Field order tracking schema for Neon (PostgreSQL).
-- Run in the Neon SQL Editor. Uses INR paise to avoid floating point money.

CREATE TABLE IF NOT EXISTS customers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone_e164 TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_accounts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    phone_e164 TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'customer' CHECK (role IN ('customer','admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_number TEXT NOT NULL UNIQUE,
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL DEFAULT 'Order placed' CHECK (status IN (
        'Order placed', 'Confirmed', 'Processing', 'Packed', 'Shipped',
        'In transit', 'Out for delivery', 'Delivered', 'Cancellation requested',
        'Cancelled', 'Return requested', 'Return approved', 'Return rejected',
        'Return picked up', 'Returned', 'Refund pending', 'Refund completed'
    )),
    payment_method TEXT NOT NULL DEFAULT 'Cash on delivery',
    payment_status TEXT NOT NULL DEFAULT 'Pending' CHECK (payment_status IN
        ('Pending', 'Paid', 'Failed', 'Refund pending', 'Refunded')),
    currency CHAR(3) NOT NULL DEFAULT 'INR',
    subtotal_paise BIGINT NOT NULL CHECK (subtotal_paise >= 0),
    shipping_paise BIGINT NOT NULL DEFAULT 0 CHECK (shipping_paise >= 0),
    total_paise BIGINT NOT NULL CHECK (total_paise >= 0),
    shipping_address JSONB NOT NULL,
    placed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    estimated_delivery_from DATE NOT NULL,
    estimated_delivery_to DATE NOT NULL,
    requested_delivery_date DATE NOT NULL,
    requested_delivery_slot TEXT NOT NULL CHECK (requested_delivery_slot IN ('09:00-12:00','12:00-15:00','15:00-18:00','18:00-21:00')),
    promised_delivery_date DATE,
    delivered_at TIMESTAMPTZ,
    return_window_ends_at DATE,
    cancellation_requested_at TIMESTAMPTZ,
    cancellation_reason TEXT,
    cancelled_at TIMESTAMPTZ,
    idempotency_key TEXT UNIQUE,
    notification_status JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (estimated_delivery_to >= estimated_delivery_from)
);

ALTER TABLE orders ADD COLUMN IF NOT EXISTS requested_delivery_date DATE NOT NULL DEFAULT (CURRENT_DATE + 5);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS requested_delivery_slot TEXT NOT NULL DEFAULT '09:00-12:00';
ALTER TABLE orders ALTER COLUMN requested_delivery_date DROP DEFAULT;
ALTER TABLE orders ALTER COLUMN requested_delivery_slot DROP DEFAULT;

CREATE TABLE IF NOT EXISTS order_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '',
    size TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_paise BIGINT NOT NULL CHECK (unit_price_paise >= 0),
    line_total_paise BIGINT NOT NULL CHECK (line_total_paise >= 0),
    image_url TEXT
);

CREATE TABLE IF NOT EXISTS order_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    description TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT 'system',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS return_requests (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_number TEXT NOT NULL UNIQUE,
    order_id BIGINT NOT NULL REFERENCES orders(id),
    status TEXT NOT NULL DEFAULT 'Requested' CHECK (status IN (
        'Requested', 'Pending executive approval', 'Approved', 'Rejected',
        'Pickup scheduled', 'Received', 'Inspected', 'Refund pending', 'Refunded'
    )),
    reason TEXT NOT NULL,
    customer_notes TEXT NOT NULL DEFAULT '',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ,
    inspected_at TIMESTAMPTZ,
    refund_amount_paise BIGINT CHECK (refund_amount_paise >= 0),
    refund_reference TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS return_request_items (
    return_request_id BIGINT NOT NULL REFERENCES return_requests(id) ON DELETE CASCADE,
    order_item_id BIGINT NOT NULL REFERENCES order_items(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (return_request_id, order_item_id)
);

CREATE INDEX IF NOT EXISTS orders_customer_placed_idx ON orders(customer_id, placed_at DESC);
CREATE INDEX IF NOT EXISTS orders_status_delivery_idx ON orders(status, estimated_delivery_to);
CREATE INDEX IF NOT EXISTS orders_phone_lookup_idx ON customers(phone_e164);
CREATE INDEX IF NOT EXISTS order_events_order_time_idx ON order_events(order_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS return_requests_order_status_idx ON return_requests(order_id, status);
