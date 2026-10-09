"""The tag history route says what made each change (migration m0010; THREAT_MODEL T-504; #229).
Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.storage import accounts as ac
from coinacct.storage.accounts import ME

from .test_tags_api import SCRIPT, World


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.db.close()


def test_the_history_names_what_made_each_change_t504(world: World) -> None:
    wallet = ac.add_tax_account(world.db, "Cold storage", "self_custody")
    phone = ac.add_client(world.db, "Phone", "mobile")
    for clients in ([], [phone]):  # an import adds the address, then edits it
        ac.add_addresses(
            world.db,
            [(SCRIPT, None)],
            entity_id=ME,
            tax_account_id=wallet,
            source="import",
            client_ids=clients,
        )
    world.post(
        "/api/tags/address", {"script": SCRIPT, "entity_id": ME, "tax_account_id": wallet, "label": "x"}
    )
    changes = world.post("/api/tags/history", {"kind": "address_tag", "subject": SCRIPT}).json()["changes"]
    assert [c["origin"] for c in changes] == ["import", "user"]
