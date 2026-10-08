"""Who owns what: entities, tax accounts, wallet clients, addresses and public descriptors (PLAN §2;
ADR 0008, ADR 0019; THREAT_MODEL T-701, T-703).

- An **entity** is anyone who can own coins: the user ("Me", seeded here as id 1, the only `self`),
  an exchange, an employer. `knows_identity` marks the ones that can link coins to the user (doxx
  propagation, ADR 0010).
- A **tax account** is the unit of per-account basis (ADR 0008): a self-custody wallet, or a custodial
  account at an exchange (with its custodian, and whether the exchange issues the user a 1099-DA).
  It records its standing identification method.
- A **wallet client** is a label only (a hardware wallet, a mobile app), many-to-many with addresses
  and descriptors.
- An **address** is keyed by its output script, so P2PK and bare multisig work, and the same script
  imported twice is one address. **It is the one record of who owns a script:** an owned address
  belongs to exactly one self-custody tax account, any other to none. A descriptor's derived scripts
  are address rows too, with the descriptor's owner and account, so no script can belong to two
  accounts or two owners.
- A **descriptor** is public only. Its `descriptor_script` rows are the script-to-index table that
  maps scan hits back to derivation indexes, within its window (`range_end`).

The writers (`storage.accounts`) also refuse private key material, as a backstop to the import
service (`domain.keys`; T-703).
"""

SQL = """
CREATE TABLE entity (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 200),
    kind TEXT NOT NULL CHECK (kind IN ('self', 'exchange', 'employer', 'merchant', 'person', 'unknown')),
    knows_identity INTEGER NOT NULL CHECK (knows_identity IN (0, 1)),
    notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 10000),
    -- The user is entity 1, and the only one of kind 'self': owned means owned by entity 1.
    CHECK ((kind = 'self') = (id = 1))
) STRICT;
INSERT INTO entity (id, name, kind, knows_identity) VALUES (1, 'Me', 'self', 0);
CREATE TRIGGER entity_me_stays BEFORE DELETE ON entity WHEN OLD.id = 1
BEGIN
    SELECT RAISE(ABORT, 'the user entity can''t be removed');
END;

CREATE TABLE tax_account (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 200),
    kind TEXT NOT NULL CHECK (kind IN ('self_custody', 'custodial')),
    -- The custodian, for a custodial account; none for self custody.
    entity_id INTEGER REFERENCES entity (id) ON DELETE RESTRICT,
    -- The standing order on file, if any. None is not FIFO: it means the statutory FIFO fallback
    -- applies, which the tax engine and the late-choice warnings tell apart (ADR 0008, ADR 0021).
    standing_method TEXT CHECK (standing_method IS NULL OR standing_method IN ('fifo', 'specific')),
    automatic INTEGER NOT NULL DEFAULT 1 CHECK (automatic IN (0, 1)),
    -- Whether the custodian issues the user a Form 1099-DA for this account (PLAN §2, §8).
    issues_1099da INTEGER NOT NULL DEFAULT 0 CHECK (issues_1099da IN (0, 1)),
    CHECK ((kind = 'custodial') = (entity_id IS NOT NULL)),
    CHECK (entity_id IS NULL OR entity_id != 1),
    CHECK (kind = 'custodial' OR issues_1099da = 0)
) STRICT;
-- A custodial account's custodian is an exchange (PLAN §2).
CREATE TRIGGER tax_account_custodian_is_an_exchange BEFORE INSERT ON tax_account
WHEN NEW.entity_id IS NOT NULL AND (SELECT kind FROM entity WHERE id = NEW.entity_id) IS NOT 'exchange'
BEGIN
    SELECT RAISE(ABORT, 'a custodial account''s custodian is an exchange');
END;
CREATE TRIGGER tax_account_custodian_is_an_exchange_on_update BEFORE UPDATE OF entity_id ON tax_account
WHEN NEW.entity_id IS NOT NULL AND (SELECT kind FROM entity WHERE id = NEW.entity_id) IS NOT 'exchange'
BEGIN
    SELECT RAISE(ABORT, 'a custodial account''s custodian is an exchange');
END;
CREATE TRIGGER entity_custodian_stays_an_exchange BEFORE UPDATE OF kind ON entity
WHEN NEW.kind != 'exchange' AND EXISTS (SELECT 1 FROM tax_account WHERE entity_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'a custodian stays an exchange');
END;

CREATE TABLE wallet_client (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 200),
    kind TEXT NOT NULL CHECK (kind IN ('hardware', 'mobile', 'desktop', 'web', 'paper', 'other'))
) STRICT;

CREATE TABLE address (
    script_hex TEXT NOT NULL PRIMARY KEY CHECK (length(script_hex) BETWEEN 2 AND 20000
        AND length(script_hex) % 2 = 0 AND script_hex NOT GLOB '*[^0-9a-f]*'),
    -- As the user's wallets show it, when the script has an address form.
    text TEXT CHECK (text IS NULL OR length(text) BETWEEN 1 AND 90),
    entity_id INTEGER NOT NULL REFERENCES entity (id) ON DELETE RESTRICT,
    tax_account_id INTEGER REFERENCES tax_account (id) ON DELETE RESTRICT,
    label TEXT NOT NULL DEFAULT '' CHECK (length(label) <= 200),
    source TEXT NOT NULL CHECK (source IN ('import', 'descriptor', 'discovery', 'manual')),
    confirmed INTEGER NOT NULL DEFAULT 1 CHECK (confirmed IN (0, 1)),
    start_height INTEGER NOT NULL DEFAULT 0 CHECK (start_height >= 0)
) STRICT, WITHOUT ROWID;
CREATE INDEX address_by_entity ON address (entity_id);
CREATE INDEX address_by_tax_account ON address (tax_account_id);

-- Owned addresses (the user's) belong to exactly one self-custody tax account; others to none.
-- A custodial account holds coins at the exchange, on the exchange's addresses (PLAN §2).
CREATE TRIGGER address_owner_and_account BEFORE INSERT ON address
WHEN (NEW.entity_id = 1) != (NEW.tax_account_id IS NOT NULL)
    OR (NEW.tax_account_id IS NOT NULL
        AND (SELECT kind FROM tax_account WHERE id = NEW.tax_account_id) != 'self_custody')
BEGIN
    SELECT RAISE(ABORT, 'an owned address belongs to exactly one self-custody account, any other to none');
END;
CREATE TRIGGER address_owner_and_account_on_update BEFORE UPDATE OF entity_id, tax_account_id ON address
WHEN (NEW.entity_id = 1) != (NEW.tax_account_id IS NOT NULL)
    OR (NEW.tax_account_id IS NOT NULL
        AND (SELECT kind FROM tax_account WHERE id = NEW.tax_account_id) != 'self_custody')
BEGIN
    SELECT RAISE(ABORT, 'an owned address belongs to exactly one self-custody account, any other to none');
END;
-- A descriptor's scripts take its owner and account; changing one of them alone would split them.
CREATE TRIGGER address_of_a_descriptor_keeps_its_owner BEFORE UPDATE OF entity_id, tax_account_id ON address
WHEN EXISTS (SELECT 1 FROM descriptor_script WHERE script_hex = NEW.script_hex)
BEGIN
    SELECT RAISE(ABORT, 'a descriptor''s address changes owner only with its descriptor');
END;

CREATE TABLE descriptor (
    id INTEGER PRIMARY KEY,
    -- As Core normalises it (`getdescriptorinfo`), with its checksum.
    text TEXT NOT NULL UNIQUE CHECK (length(text) BETWEEN 1 AND 4000),
    entity_id INTEGER NOT NULL REFERENCES entity (id) ON DELETE RESTRICT,
    tax_account_id INTEGER REFERENCES tax_account (id) ON DELETE RESTRICT,
    label TEXT NOT NULL DEFAULT '' CHECK (length(label) <= 200),
    gap_limit INTEGER NOT NULL CHECK (gap_limit BETWEEN 1 AND 1000),
    -- Derived indexes 0..range_end are in descriptor_script; the highest one seen on chain, if any.
    range_end INTEGER NOT NULL CHECK (range_end BETWEEN 0 AND 100000),
    highest_used INTEGER CHECK (highest_used IS NULL OR highest_used BETWEEN 0 AND range_end),
    -- Where its history starts (a wallet's birthday height, or 0).
    start_height INTEGER NOT NULL DEFAULT 0 CHECK (start_height >= 0)
) STRICT;
CREATE TRIGGER descriptor_owner_and_account BEFORE INSERT ON descriptor
WHEN (NEW.entity_id = 1) != (NEW.tax_account_id IS NOT NULL)
    OR (NEW.tax_account_id IS NOT NULL
        AND (SELECT kind FROM tax_account WHERE id = NEW.tax_account_id) != 'self_custody')
BEGIN
    SELECT RAISE(ABORT, 'an owned descriptor belongs to exactly one self-custody account, any other to none');
END;
-- Its owner and account are fixed: they are its scripts' too (a change is a re-import, later).
CREATE TRIGGER descriptor_owner_is_fixed BEFORE UPDATE OF entity_id, tax_account_id ON descriptor
BEGIN
    SELECT RAISE(ABORT, 'a descriptor''s owner and account don''t change');
END;

CREATE TABLE descriptor_script (
    descriptor_id INTEGER NOT NULL REFERENCES descriptor (id) ON DELETE CASCADE,
    idx INTEGER NOT NULL CHECK (idx >= 0),
    script_hex TEXT NOT NULL REFERENCES address (script_hex) ON DELETE RESTRICT,
    -- One index can derive several scripts (combo()), but a script has one index per descriptor.
    PRIMARY KEY (descriptor_id, idx, script_hex),
    UNIQUE (descriptor_id, script_hex)
) STRICT, WITHOUT ROWID;
CREATE INDEX descriptor_script_by_script ON descriptor_script (script_hex);
-- A derived script is an address with the descriptor's owner and account, at an index in its window.
CREATE TRIGGER descriptor_script_matches BEFORE INSERT ON descriptor_script
WHEN NEW.idx > (SELECT range_end FROM descriptor WHERE id = NEW.descriptor_id)
    OR NOT EXISTS (
        SELECT 1 FROM address a JOIN descriptor d ON d.id = NEW.descriptor_id
        WHERE a.script_hex = NEW.script_hex AND a.entity_id = d.entity_id
            AND a.tax_account_id IS d.tax_account_id
    )
BEGIN
    SELECT RAISE(ABORT, 'a derived script belongs to its descriptor''s owner and account, within its window');
END;
CREATE TRIGGER descriptor_script_is_fixed BEFORE UPDATE ON descriptor_script
BEGIN
    SELECT RAISE(ABORT, 'a derived script doesn''t change');
END;

CREATE TABLE address_client (
    script_hex TEXT NOT NULL REFERENCES address (script_hex) ON DELETE CASCADE,
    client_id INTEGER NOT NULL REFERENCES wallet_client (id) ON DELETE CASCADE,
    PRIMARY KEY (script_hex, client_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE descriptor_client (
    descriptor_id INTEGER NOT NULL REFERENCES descriptor (id) ON DELETE CASCADE,
    client_id INTEGER NOT NULL REFERENCES wallet_client (id) ON DELETE CASCADE,
    PRIMARY KEY (descriptor_id, client_id)
) STRICT, WITHOUT ROWID;
"""
