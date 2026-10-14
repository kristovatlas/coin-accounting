"""Migration 11: the user's events and lot identifications (PLAN §2 `event`, `identification`; THREAT_MODEL
T-408, T-501, T-508).

- `event` holds the user's typed ledger entries: acquisitions (`buy`, `p2p_buy`, `income`, `gift_in`,
  `inherit`), movements (`self_transfer`, `deposit`, `withdrawal`) and disposals (`sell`, `spend`,
  `gift_out`). Each has its account (a movement also the account it goes to), its UTC moment, its sats, a
  USD amount (an acquisition's basis or FMV, a disposal's proceeds net of costs), the valuation's source,
  the fee sats it bears, and an optional txid. A gift received adds the donor's date, the FMV at the gift
  and whether the donor's lot was always long-term; a withdrawal from an account without lots adds the
  basis and date of the sats it has none for. PLAN §2's other kinds, `opening_allocation_2025` and
  `fee` (a fee on its own line), are in the kind list now, so `event` needn't be rebuilt for them: the
  module refuses them until their steps, which add their columns with `ALTER TABLE … ADD COLUMN` and
  their cross-field rules as triggers. A rebuild of `event` would have to carry its `sqlite_sequence`
  row over, or removed ids could be given out again.
- `identification` holds a disposal's or movement's chosen lots, in order: the engine's lot id, the sats,
  and when the user chose them (T-508). A chosen set is replaced whole, by the app, with a change-log row.
  Triggers keep it on a disposal or movement, holding no more sats than the event as rows go in, and
  covering them exactly (a movement's: or less its fee) once the event is edited; that a new choice
  covers them exactly is the module's check, since its rows go in one at a time. Event
  ids are never reused (`AUTOINCREMENT`), so a choice naming a removed event's lot can't come to name
  another's: the engine refuses it instead.
- The change log gains the kinds `event` and `identification`, so every edit to them is logged (T-408),
  and, so that it needn't be rebuilt again, the other kinds PLAN §2 says it records: `override` (a doxx
  override), `lot_link`, `tracking_start` and `wallet_fifo` (ADR 0041's links, tracking start dates and
  whole-wallet FIFO setting), for their later steps.
  SQLite can't change a CHECK in place, so the log is rebuilt as SQLite documents: a new table with the
  same columns and every rule of the old one, the rows copied with their ids, the old one dropped, the
  new one renamed, and m0006 to m0010's triggers recreated unchanged. Every value is kept (ADR 0038: a
  rebuild that keeps every value is no alteration), and the rules together refuse as much as before, plus
  nothing but the new kinds.
"""

SQL = """
CREATE TABLE change_log_m0011 (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL CHECK (length(at) BETWEEN 20 AND 40),
    kind TEXT NOT NULL CHECK (kind IN (
        'address_tag', 'tx_flag', 'event', 'identification',
        'override', 'lot_link', 'tracking_start', 'wallet_fifo'
    )),
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
    -- AUTOINCREMENT: a removed event's id is never given to another, so a choice of lots that named it
    -- can't come to name a different purchase (https://www.sqlite.org/autoinc.html)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN (
        'buy', 'p2p_buy', 'income', 'gift_in', 'inherit', 'opening_allocation_2025',
        'self_transfer', 'deposit', 'withdrawal',
        'sell', 'spend', 'gift_out', 'fee'
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
        AND usd NOT GLOB '*[^0-9.-]*' AND substr(usd, 2) NOT GLOB '*-*')),
    valuation_source TEXT CHECK (valuation_source IS NULL OR length(valuation_source) BETWEEN 1 AND 200),
    txid TEXT CHECK (txid IS NULL OR (length(txid) = 64 AND txid NOT GLOB '*[^0-9a-f]*')),
    fmv TEXT CHECK (fmv IS NULL OR (length(fmv) BETWEEN 1 AND 24 AND fmv GLOB '*[0-9]*'
        AND fmv NOT GLOB '*[^0-9.-]*' AND substr(fmv, 2) NOT GLOB '*-*')),
    donor_acquired TEXT CHECK (donor_acquired IS NULL OR (length(donor_acquired) = 10
        AND donor_acquired GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')),
    donor_always_long INTEGER NOT NULL DEFAULT 0 CHECK (donor_always_long IN (0, 1)),
    missing_basis TEXT CHECK (missing_basis IS NULL OR (length(missing_basis) BETWEEN 1 AND 24
        AND missing_basis GLOB '*[0-9]*' AND missing_basis NOT GLOB '*[^0-9.-]*'
        AND substr(missing_basis, 2) NOT GLOB '*-*')),
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
    -- a BTC fee is borne by a movement, or a buy whose fee was withheld from the sats bought (ADR 0043:
    -- the lot holds the sats credited, its basis the full cash paid); a disposal's waits for the engine
    CHECK (
        fee_sats = 0 OR kind IN ('buy', 'self_transfer', 'deposit', 'withdrawal')
    ),
    -- a gift given has no proceeds (ADR 0009)
    CHECK (kind != 'gift_out' OR usd = '0.00')
) STRICT;
CREATE INDEX event_by_moment ON event (at, id);

CREATE TABLE identification (
    event_id INTEGER NOT NULL REFERENCES event (id) ON DELETE RESTRICT,
    position INTEGER NOT NULL CHECK (position >= 0),
    lot TEXT NOT NULL CHECK (length(lot) BETWEEN 1 AND 400),
    sats INTEGER NOT NULL CHECK (sats BETWEEN 1 AND 2100000000000000),
    -- PLAN §2's method and late flag: a stored choice is always the user's specific one (the standing
    -- order and the FIFO fallback apply where none is stored); `late` is the engine's judgment, kept
    -- for the audit trail once services record it (0 until then)
    method TEXT NOT NULL DEFAULT 'specific' CHECK (method IN ('specific', 'standing_order', 'fifo_default')),
    late INTEGER NOT NULL DEFAULT 0 CHECK (late IN (0, 1)),
    identified_at TEXT NOT NULL CHECK (
        length(identified_at) BETWEEN 20 AND 40
        AND identified_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*'
    ),
    PRIMARY KEY (event_id, position)
) STRICT, WITHOUT ROWID;
CREATE TRIGGER identification_of_a_disposal_or_movement BEFORE INSERT ON identification
WHEN (SELECT kind FROM event WHERE id = NEW.event_id) NOT IN (
    'self_transfer', 'deposit', 'withdrawal', 'sell', 'spend', 'gift_out'
)
BEGIN SELECT RAISE(ABORT, 'only a disposal or a movement chooses lots'); END;
CREATE TRIGGER identification_within_its_events_sats BEFORE INSERT ON identification
WHEN NEW.sats + (SELECT coalesce(sum(sats), 0) FROM identification WHERE event_id = NEW.event_id)
    > (SELECT sats FROM event WHERE id = NEW.event_id)
BEGIN SELECT RAISE(ABORT, 'the chosen lots hold more sats than the event'); END;
CREATE TRIGGER identification_is_replaced_never_edited BEFORE UPDATE ON identification
BEGIN SELECT RAISE(ABORT, 'a choice of lots is replaced, never edited'); END;
CREATE TRIGGER identification_not_on_a_missing_basis BEFORE INSERT ON identification
WHEN (SELECT missing_basis FROM event WHERE id = NEW.event_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'a withdrawal with a missing basis chooses no lots'); END;
CREATE TRIGGER event_with_chosen_lots_keeps_its_account_moment_and_basis
BEFORE UPDATE OF tax_account_id, at, missing_basis ON event
WHEN EXISTS (SELECT 1 FROM identification WHERE event_id = NEW.id)
    AND (NEW.tax_account_id <> OLD.tax_account_id OR NEW.at <> OLD.at OR NEW.missing_basis IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'an event with a choice of lots keeps its account, moment and basis'); END;
CREATE TRIGGER event_with_chosen_lots_stays_a_disposal_or_movement BEFORE UPDATE OF kind ON event
WHEN NEW.kind NOT IN ('self_transfer', 'deposit', 'withdrawal', 'sell', 'spend', 'gift_out')
    AND EXISTS (SELECT 1 FROM identification WHERE event_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'only a disposal or a movement chooses lots'); END;
CREATE TRIGGER event_keeps_the_sats_its_lots_were_chosen_for BEFORE UPDATE OF sats, fee_sats, kind ON event
WHEN EXISTS (SELECT 1 FROM identification WHERE event_id = NEW.id)
    AND (SELECT sum(sats) FROM identification WHERE event_id = NEW.id) <> NEW.sats
    AND NOT (
        NEW.kind IN ('deposit', 'withdrawal', 'self_transfer') AND NEW.fee_sats > 0
        AND (SELECT sum(sats) FROM identification WHERE event_id = NEW.id) = NEW.sats - NEW.fee_sats
    )
BEGIN SELECT RAISE(ABORT, 'the chosen lots no longer cover the event''s sats'); END;
"""
