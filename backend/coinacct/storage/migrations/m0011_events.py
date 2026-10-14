"""Migration 11: the user's events and lot identifications (PLAN §2 `event`, `identification`; THREAT_MODEL
T-408, T-501, T-508).

- `event` holds the user's typed ledger entries: acquisitions (`buy`, `p2p_buy`, `income`, `gift_in`,
  `inherit`), movements (`self_transfer`, `deposit`, `withdrawal`) and disposals (`sell`, `spend`,
  `gift_out`). Each has its account (a movement also the account it goes to), its UTC moment, its sats, a
  USD amount (an acquisition's basis or FMV, a disposal's proceeds net of costs), the valuation's source,
  the fee sats it bears, and an optional txid. A gift received adds the donor's date, the FMV at the gift
  and whether the donor's lot was always long-term; a withdrawal from an account without lots adds the
  basis and date of the sats it has none for. The opening allocation, a fee on its own line and lot links
  come in later steps.
- `identification` holds a disposal's or movement's chosen lots, in order: the engine's lot id, the sats,
  and when the user chose them (T-508). A chosen set is replaced whole, by the app, with a change-log row.
- The change log gains the kind `event` (and `identification`), so every edit to them is logged (T-408).
  SQLite can't change a CHECK in place, so the log is rebuilt as SQLite documents: a new table with the
  same columns and every rule of the old one, the rows copied with their ids, the old one dropped, the
  new one renamed, and m0006 to m0010's triggers recreated unchanged. Every value is kept (ADR 0038: a
  rebuild that keeps every value is no alteration), and the rules together refuse as much as before, plus
  nothing but the two new kinds.
"""

SQL = """
CREATE TABLE change_log_m0011 (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL CHECK (length(at) BETWEEN 20 AND 40),
    kind TEXT NOT NULL CHECK (kind IN ('address_tag', 'tx_flag', 'event', 'identification')),
    subject TEXT NOT NULL CHECK (length(subject) BETWEEN 1 AND 20000),
    before TEXT CHECK (before IS NULL OR json_valid(before)),
    after TEXT NOT NULL CHECK (json_valid(after)),
    origin TEXT CHECK (
        origin IS NULL OR (
            origin IN ('user', 'import', 'descriptor', 'discovery', 'heuristic', 'suggestion')
            AND (origin <> 'heuristic' OR kind = 'tx_flag')
        )
    )
) STRICT;
INSERT INTO change_log_m0011 (id, at, kind, subject, before, after, origin)
    SELECT id, at, kind, subject, before, after, origin FROM change_log ORDER BY id;
CREATE TEMP TABLE m0011_check (
    copied INTEGER NOT NULL CONSTRAINT change_log_copied_whole CHECK (copied = 1)
);
INSERT INTO m0011_check SELECT
    (SELECT count(*) FROM change_log) = (SELECT count(*) FROM change_log_m0011)
    AND NOT EXISTS (
        SELECT id, at, kind, subject, before, after, origin FROM change_log
        EXCEPT SELECT id, at, kind, subject, before, after, origin FROM change_log_m0011
    );
DROP TABLE m0011_check;
DROP TABLE change_log;
ALTER TABLE change_log_m0011 RENAME TO change_log;

CREATE TRIGGER change_log_is_append_only_update BEFORE UPDATE ON change_log
BEGIN
    SELECT RAISE(ABORT, 'the change log is append-only');
END;
CREATE TRIGGER change_log_is_append_only_delete BEFORE DELETE ON change_log
BEGIN
    SELECT RAISE(ABORT, 'the change log is append-only');
END;
CREATE TRIGGER change_log_no_replace BEFORE INSERT ON change_log
WHEN NEW.id >= 1 AND EXISTS (SELECT 1 FROM change_log WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'the change log is append-only'); END;
CREATE TRIGGER change_log_ids_are_positive AFTER INSERT ON change_log
WHEN NEW.id < 1
BEGIN SELECT RAISE(ABORT, 'the change log is append-only'); END;
CREATE TRIGGER change_log_names_its_origin BEFORE INSERT ON change_log
WHEN NEW.origin IS NULL
BEGIN SELECT RAISE(ABORT, 'a change-log row names its origin'); END;

CREATE TABLE event (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN (
        'buy', 'p2p_buy', 'income', 'gift_in', 'inherit',
        'self_transfer', 'deposit', 'withdrawal',
        'sell', 'spend', 'gift_out'
    )),
    tax_account_id INTEGER NOT NULL REFERENCES tax_account (id) ON DELETE RESTRICT,
    to_account_id INTEGER REFERENCES tax_account (id) ON DELETE RESTRICT,
    -- the moment, timezone-aware UTC, ISO 8601 ("2025-03-01T12:00:00+00:00")
    at TEXT NOT NULL CHECK (
        length(at) BETWEEN 20 AND 40 AND at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*'
    ),
    sats INTEGER NOT NULL CHECK (sats BETWEEN 1 AND 2100000000000000),
    fee_sats INTEGER NOT NULL DEFAULT 0 CHECK (fee_sats >= 0 AND fee_sats < sats),
    -- USD as text, exact cents: an acquisition's basis or FMV, a disposal's proceeds (None: a gift's
    -- unknown donor basis, or a movement)
    usd TEXT CHECK (usd IS NULL OR (length(usd) BETWEEN 1 AND 24 AND usd GLOB '*[0-9]*'
        AND usd NOT GLOB '*[^0-9.-]*')),
    valuation_source TEXT CHECK (valuation_source IS NULL OR length(valuation_source) BETWEEN 1 AND 200),
    txid TEXT CHECK (txid IS NULL OR (length(txid) = 64 AND txid NOT GLOB '*[^0-9a-f]*')),
    fmv TEXT CHECK (fmv IS NULL OR (length(fmv) BETWEEN 1 AND 24 AND fmv NOT GLOB '*[^0-9.-]*')),
    donor_acquired TEXT CHECK (donor_acquired IS NULL OR (length(donor_acquired) = 10
        AND donor_acquired GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')),
    donor_always_long INTEGER NOT NULL DEFAULT 0 CHECK (donor_always_long IN (0, 1)),
    missing_basis TEXT CHECK (missing_basis IS NULL OR (length(missing_basis) BETWEEN 1 AND 24
        AND missing_basis NOT GLOB '*[^0-9.-]*')),
    missing_acquired TEXT CHECK (missing_acquired IS NULL OR (length(missing_acquired) = 10
        AND missing_acquired GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')),
    note TEXT CHECK (note IS NULL OR length(note) BETWEEN 1 AND 2000),
    -- a movement goes to another account; nothing else names one
    CHECK ((kind IN ('self_transfer', 'deposit', 'withdrawal')) = (to_account_id IS NOT NULL)),
    CHECK (to_account_id IS NULL OR to_account_id != tax_account_id),
    -- only a movement has no USD amount; only a gift received may lack one (an unknown donor basis)
    CHECK ((kind IN ('self_transfer', 'deposit', 'withdrawal')) = (usd IS NULL) OR kind = 'gift_in'),
    -- a gift received names its FMV and the donor's date; nothing else does
    CHECK ((kind = 'gift_in') = (fmv IS NOT NULL AND donor_acquired IS NOT NULL)),
    CHECK (kind = 'gift_in' OR (fmv IS NULL AND donor_acquired IS NULL AND donor_always_long = 0)),
    -- only a withdrawal names the basis of sats its account has no lots for, and then both parts
    CHECK (kind = 'withdrawal' OR (missing_basis IS NULL AND missing_acquired IS NULL)),
    CHECK ((missing_basis IS NULL) = (missing_acquired IS NULL)),
    -- a fee is borne by a movement or a disposal
    CHECK (fee_sats = 0 OR kind IN ('self_transfer', 'deposit', 'withdrawal', 'sell', 'spend', 'gift_out'))
) STRICT;
CREATE INDEX event_by_moment ON event (at, id);

CREATE TABLE identification (
    event_id INTEGER NOT NULL REFERENCES event (id) ON DELETE RESTRICT,
    position INTEGER NOT NULL CHECK (position >= 0),
    lot TEXT NOT NULL CHECK (length(lot) BETWEEN 1 AND 400),
    sats INTEGER NOT NULL CHECK (sats BETWEEN 1 AND 2100000000000000),
    identified_at TEXT NOT NULL CHECK (length(identified_at) BETWEEN 20 AND 40),
    PRIMARY KEY (event_id, position)
) STRICT, WITHOUT ROWID;
CREATE TRIGGER identification_of_a_disposal_or_movement BEFORE INSERT ON identification
WHEN (SELECT kind FROM event WHERE id = NEW.event_id) NOT IN (
    'self_transfer', 'deposit', 'withdrawal', 'sell', 'spend', 'gift_out'
)
BEGIN SELECT RAISE(ABORT, 'only a disposal or a movement chooses lots'); END;
"""
