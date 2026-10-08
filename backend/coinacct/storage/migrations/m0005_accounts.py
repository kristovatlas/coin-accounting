"""Who owns what: entities, tax accounts, wallet clients, addresses and public descriptors (PLAN §2;
ADR 0008, ADR 0019; THREAT_MODEL T-701, T-703).

- An **entity** is anyone who can own coins: the user ("me", seeded here), an exchange, an employer.
  `knows_identity` marks the ones that can link coins to the user (doxx propagation, ADR 0010).
- A **tax account** is the unit of per-account basis (ADR 0008): a self-custody wallet or a custodial
  account at an exchange. It records its standing identification method.
- A **wallet client** is a label only (a hardware wallet, a mobile app), many-to-many with addresses
  and descriptors.
- An **address** is keyed by its output script, so P2PK and bare multisig work, and the same script
  imported twice is one address. An owned address belongs to exactly one tax account.
- A **descriptor** is public only (`domain.keys` refuses private material before it gets here). Its
  `descriptor_script` rows are the script-to-index table that maps scan hits back to derivation
  indexes.
"""

SQL = """
CREATE TABLE entity (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 200),
    kind TEXT NOT NULL CHECK (kind IN ('self', 'exchange', 'employer', 'merchant', 'person', 'unknown')),
    knows_identity INTEGER NOT NULL CHECK (knows_identity IN (0, 1)),
    notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 10000)
) STRICT;
-- The user. Owned addresses and self-custody accounts belong to it.
INSERT INTO entity (id, name, kind, knows_identity) VALUES (1, 'Me', 'self', 0);

CREATE TABLE tax_account (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 200),
    kind TEXT NOT NULL CHECK (kind IN ('self_custody', 'custodial')),
    -- The custodian, for a custodial account; none for self custody.
    entity_id INTEGER REFERENCES entity (id) ON DELETE RESTRICT,
    standing_method TEXT NOT NULL DEFAULT 'fifo' CHECK (standing_method IN ('fifo', 'specific')),
    automatic INTEGER NOT NULL DEFAULT 1 CHECK (automatic IN (0, 1)),
    CHECK ((kind = 'custodial') = (entity_id IS NOT NULL))
) STRICT;

CREATE TABLE wallet_client (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 200),
    kind TEXT NOT NULL CHECK (kind IN ('hardware', 'mobile', 'desktop', 'web', 'paper', 'other'))
) STRICT;

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

CREATE TABLE descriptor_script (
    descriptor_id INTEGER NOT NULL REFERENCES descriptor (id) ON DELETE CASCADE,
    idx INTEGER NOT NULL CHECK (idx >= 0),
    script_hex TEXT NOT NULL CHECK (length(script_hex) BETWEEN 2 AND 20000 AND length(script_hex) % 2 = 0
        AND script_hex NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY (descriptor_id, idx),
    UNIQUE (descriptor_id, script_hex)
) STRICT, WITHOUT ROWID;
CREATE INDEX descriptor_script_by_script ON descriptor_script (script_hex);

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

-- Owned addresses (the user's) belong to exactly one tax account; others to none (PLAN §2).
CREATE TRIGGER address_owned_has_an_account BEFORE INSERT ON address
WHEN (NEW.entity_id = 1) != (NEW.tax_account_id IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'an owned address belongs to exactly one tax account, any other to none');
END;
CREATE TRIGGER address_owned_has_an_account_on_update BEFORE UPDATE OF entity_id, tax_account_id ON address
WHEN (NEW.entity_id = 1) != (NEW.tax_account_id IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'an owned address belongs to exactly one tax account, any other to none');
END;
CREATE TRIGGER descriptor_owned_has_an_account BEFORE INSERT ON descriptor
WHEN (NEW.entity_id = 1) != (NEW.tax_account_id IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'an owned descriptor belongs to exactly one tax account, any other to none');
END;
CREATE TRIGGER descriptor_owned_has_an_account_on_update
BEFORE UPDATE OF entity_id, tax_account_id ON descriptor
WHEN (NEW.entity_id = 1) != (NEW.tax_account_id IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'an owned descriptor belongs to exactly one tax account, any other to none');
END;
-- The user entity is the anchor of everything owned: it stays.
CREATE TRIGGER entity_me_stays BEFORE DELETE ON entity WHEN OLD.id = 1
BEGIN
    SELECT RAISE(ABORT, 'the user entity can''t be removed');
END;
CREATE TRIGGER entity_me_stays_self BEFORE UPDATE OF id, kind ON entity WHEN OLD.id = 1
BEGIN
    SELECT RAISE(ABORT, 'the user entity stays the user');
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
