"""The doxx rules (ADR 0010; T-504, T-511), on hand-built graphs: one test per rule, and the mixing
exception."""

from __future__ import annotations

import pytest

from coinacct.doxx.engine import Coin, DoxxError, Override, Received, Tag, Tx, doxx

K = frozenset({"exch", "boss"})  # the parties that know the user's identity


def mine(outpoint: str, address: str) -> Coin:
    return Coin(outpoint, address, True)


def theirs(outpoint: str, address: str, entity: str | None = None) -> Coin:
    return Coin(outpoint, address, False, entity)


def tags(result: tuple[Tag, ...]) -> set[tuple[str, str, str, str]]:
    return {(t.outpoint, t.entity, t.confidence, t.reason) for t in result}


def test_paid_to_k_doxxes_every_owned_input_and_the_change_adr0010() -> None:
    tx = Tx("t1", (mine("a:0", "A"), mine("b:0", "B")), (theirs("t1:0", "X", "exch"), mine("t1:1", "C")))
    assert tags(doxx([tx], K)) == {
        ("a:0", "exch", "certain", "paid_to"),
        ("b:0", "exch", "certain", "paid_to"),
        ("t1:1", "exch", "certain", "paid_to"),
    }


def test_in_a_mixing_tx_paid_to_k_doxxes_the_inputs_only_as_inferred_adr0010() -> None:
    tx = Tx(
        "t1",
        (mine("a:0", "A"), theirs("z:0", "Z")),
        (theirs("t1:0", "X", "exch"), mine("t1:1", "C")),
        mixing=True,
    )
    assert tags(doxx([tx], K)) == {
        ("a:0", "exch", "inferred", "paid_to"),
        ("t1:1", "exch", "certain", "paid_to"),
    }


def test_a_party_that_doesnt_know_the_users_identity_doxxes_nothing() -> None:
    tx = Tx("t1", (mine("a:0", "A"),), (theirs("t1:0", "X", "shop"),))
    assert doxx([tx], K) == ()


def test_received_from_k_comes_from_the_recorded_event_adr0010() -> None:
    tx = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))  # the exchange's address isn't recognised
    assert tags(doxx([tx], K, [Received("t1:0", "boss")])) == {("t1:0", "boss", "certain", "received_from")}


def test_forward_owned_outputs_inherit_their_inputs_sets_adr0010() -> None:
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    t2 = Tx("t2", (mine("t1:0", "A"),), (mine("t2:0", "B"), theirs("t2:1", "Y")))
    assert tags(doxx([t1, t2], K, [Received("t1:0", "exch")])) == {
        ("t1:0", "exch", "certain", "received_from"),
        ("t2:0", "exch", "certain", "forward"),
    }  # never the third party's t2:1


def test_forward_through_a_mixing_tx_is_only_inferred_adr0010() -> None:
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    t2 = Tx(
        "t2", (mine("t1:0", "A"), theirs("z:0", "Z")), (mine("t2:0", "B"), theirs("t2:1", "Y")), mixing=True
    )
    assert ("t2:0", "exch", "inferred", "forward") in tags(doxx([t1, t2], K, [Received("t1:0", "exch")]))


def test_address_reuse_doxxes_every_coin_paid_to_the_address_adr0010() -> None:
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    t2 = Tx("t2", (theirs("f:0", "F"),), (mine("t2:0", "A"),))  # the same address A again
    assert ("t2:0", "exch", "certain", "address_reuse") in tags(doxx([t1, t2], K, [Received("t1:0", "exch")]))


def test_the_backward_cluster_infers_co_spent_addresses_adr0010() -> None:
    # A and B are co-spent in t3 (not mixing); A's coin is the exchange's: B's other coin is inferred
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    t2 = Tx("t2", (theirs("f:0", "F"),), (mine("t2:0", "B"), mine("t2:1", "B2")))
    t3 = Tx("t3", (mine("x:0", "A"), mine("y:0", "B")), (theirs("t3:0", "Y"),))
    result = tags(doxx([t1, t2, t3], K, [Received("t1:0", "exch")]))
    assert ("t2:0", "exch", "inferred", "cluster_backward") in result
    assert not any(t[0] == "t2:1" for t in result)  # B2 was never co-spent


def test_a_mixing_tx_links_no_inputs_to_each_other_adr0010() -> None:
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    t2 = Tx("t2", (theirs("f:0", "F"),), (mine("t2:0", "B"),))
    t3 = Tx("t3", (mine("x:0", "A"), mine("y:0", "B")), (theirs("t3:0", "Y"),), mixing=True)
    assert not any(t[0] == "t2:0" for t in tags(doxx([t1, t2, t3], K, [Received("t1:0", "exch")])))


def test_a_certain_tag_wins_over_an_inferred_one() -> None:
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    t2 = Tx("t2", (mine("t1:0", "A"), theirs("z:0", "Z")), (mine("t2:0", "B"),), mixing=True)  # inferred
    t3 = Tx("t3", (theirs("g:0", "G"),), (mine("t3:0", "B"),))  # B gets a certain one later, by reuse
    received = [Received("t1:0", "exch"), Received("t3:0", "exch")]
    result = tags(doxx([t1, t2, t3], K, received))
    assert ("t2:0", "exch", "certain", "address_reuse") in result


def test_doxx_sets_are_multi_valued() -> None:
    tx = Tx(
        "t1",
        (mine("a:0", "A"),),
        (theirs("t1:0", "X", "exch"), theirs("t1:1", "Y", "boss"), mine("t1:2", "C")),
    )
    assert {t.entity for t in doxx([tx], K) if t.outpoint == "t1:2"} == {"exch", "boss"}


def test_overrides_add_a_tag_that_propagates_and_remove_one_for_good() -> None:
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    t2 = Tx("t2", (mine("t1:0", "A"),), (mine("t2:0", "B"),))
    result = tags(
        doxx([t1, t2], K, overrides=[Override("t1:0", "boss", True), Override("t2:0", "boss", False)])
    )
    assert result == {("t1:0", "boss", "certain", "manual")}  # forward would have given t2:0


def test_the_result_is_the_same_whatever_the_order_of_the_received_events() -> None:
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"), mine("t1:1", "B")))
    received = [Received("t1:0", "exch"), Received("t1:1", "boss")]
    assert doxx([t1], K, received) == doxx([t1], K, received[::-1])


@pytest.mark.parametrize(
    ("args", "match"),
    [
        (([], {"exch"}), "frozenset"),
        ((["t"], K), "sequence of Tx"),
        (([Tx("", (), ())], K), "unique txid"),
        (([Tx("t", (), ()), Tx("t", (), ())], K), "unique txid"),
        (([Tx("t", [], ())], K), "tuples"),  # type: ignore[arg-type]
        (([Tx("t", (Coin("a", "A", True),), ())], K), "outpoint and address"),
        (([Tx("t", (Coin("a:0", "A", "yes"),), ())], K), "whether it is owned"),  # type: ignore[arg-type]
        (([Tx("t", (Coin("a:0", "A", True, "exch"),), ())], K), "no other entity"),
        (([], K, ["r"]), "sequence of Received"),
        (([], K, [Received("", "x")]), "outpoint and entity"),
        (([], K, (), [Override("a:0", "x", 1)]), "whether it is present"),  # type: ignore[arg-type]
    ],
)
def test_invalid_input_raises(args: tuple[object, ...], match: str) -> None:
    with pytest.raises(DoxxError, match=match):
        doxx(*args)  # type: ignore[arg-type]
