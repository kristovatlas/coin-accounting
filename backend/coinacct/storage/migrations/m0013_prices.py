"""Migration 13: daily prices (PLAN §2 `price`, §6; ADR 0039; THREAT_MODEL T-303, T-304, T-408).

- `price` holds one price per UTC day and currency: USD for tax figures, EUR and GBP for display. The
  price is decimal text above zero with at most two decimals (whole cents, as `prices.DailyPrice`
  rounds), with the method and source it came from: Coin Metrics' reference rate, Bitstamp's typical
  price, an ECB conversion, or the user's CSV import.
- `price_batch` records each refresh or import that wrote prices, in the one transaction that wrote
  them (all or nothing, T-304): when, each source's SHA-256 (a refresh's, T-303), the reference
  rate's last priced day and whether it was stale, and the review flags as JSON.

Prices aren't in the change log, whose kinds PLAN §2 lists; each batch is recorded here instead.
"""

SQL = """
CREATE TABLE price_batch (
    id INTEGER PRIMARY KEY CHECK (id >= 1),
    at TEXT NOT NULL CHECK (length(at) BETWEEN 20 AND 40),
    kind TEXT NOT NULL CHECK (kind IN ('refresh', 'import')),
    usd_through TEXT CHECK (usd_through IS NULL OR (length(usd_through) = 10 AND usd_through GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')),
    usd_stale INTEGER NOT NULL CHECK (usd_stale IN (0, 1)),
    hashes TEXT NOT NULL CHECK (json_valid(hashes) AND json_type(hashes) = 'object'),
    flags TEXT NOT NULL CHECK (json_valid(flags) AND json_type(flags) = 'object'),
    CHECK (kind = 'refresh' OR (usd_through IS NULL AND usd_stale = 0 AND hashes = '{}'))
) STRICT;
CREATE TABLE price (
    day TEXT NOT NULL CHECK (length(day) = 10 AND day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    currency TEXT NOT NULL CHECK (currency IN ('USD', 'EUR', 'GBP')),
    price TEXT NOT NULL CHECK (
        length(price) BETWEEN 4 AND 18 AND price GLOB '*[0-9].[0-9][0-9]' AND price NOT GLOB '*[^0-9.]*'
        AND instr(substr(price, 1, length(price) - 3), '.') = 0
        AND price NOT GLOB '0.00' AND price NOT GLOB '0[0-9]*'
    ),
    method TEXT NOT NULL CHECK (method IN ('reference', 'typical', 'fx', 'import')),
    source TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 200),
    batch_id INTEGER NOT NULL REFERENCES price_batch (id) ON DELETE RESTRICT,
    PRIMARY KEY (day, currency)
) STRICT, WITHOUT ROWID;
"""
