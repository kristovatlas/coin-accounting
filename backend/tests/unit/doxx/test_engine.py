"""The doxx rules (ADR 0010; T-504, T-511), on hand-built graphs: one test per rule, and the mixing
exception."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coinacct.doxx.engine import (
    Coin,
    Doxx,
    DoxxError,
    Override,
    Received,
    Suggestion,
    Tag,
    Tx,
    Unresolved,
    doxx,
)

K = frozenset({"exch", "boss"})  # the parties that know the user's identity


def mine(outpoint: str, address: str) -> Coin:
    return Coin(outpoint, address, True)


def theirs(outpoint: str, address: str, entity: str | None = None) -> Coin:
    return Coin(outpoint, address, False, entity)


def tags(result: Doxx) -> set[tuple[str, str, str, str]]:
    return {(t.outpoint, t.entity, t.confidence, t.reason) for t in result.tags}


def test_paid_to_k_doxxes_every_owned_input_and_the_change_adr0010() -> None:
    tx = Tx("t1", (mine("a:0", "A"), mine("b:0", "B")), (theirs("t1:0", "X", "exch"), mine("t1:1", "C")))
    assert tags(doxx([tx], K)) == {
        ("a:0", "exch", "certain", "paid_to"),
        ("b:0", "exch", "certain", "paid_to"),
        ("t1:1", "exch", "certain", "change_of"),
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
        ("t1:1", "exch", "certain", "change_of"),
    }


def test_a_party_that_doesnt_know_the_users_identity_doxxes_nothing() -> None:
    tx = Tx("t1", (mine("a:0", "A"),), (theirs("t1:0", "X", "shop"),))
    assert doxx([tx], K) == Doxx(())


def test_received_from_k_comes_from_the_recorded_event_adr0010() -> None:
    tx = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))  # the exchange's address isn't recognised
    assert tags(doxx([tx], K, [Received("t1:0", "boss")])) == {("t1:0", "boss", "certain", "received_from")}


def test_forward_owned_outputs_inherit_their_inputs_sets_adr0010() -> None:
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    t2 = Tx("t2", (mine("t1:0", "A"),), (mine("t2:0", "B"), theirs("t2:1", "Y")))
    assert tags(doxx([t1, t2], K, [Received("t1:0", "exch")])) == {
        ("t1:0", "exch", "certain", "received_from"),
        ("t2:0", "exch", "certain", "change_of"),
    }  # never the third party's t2:1


def test_forward_through_a_mixing_tx_is_only_inferred_adr0010() -> None:
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    t2 = Tx(
        "t2", (mine("t1:0", "A"), theirs("z:0", "Z")), (mine("t2:0", "B"), theirs("t2:1", "Y")), mixing=True
    )
    assert ("t2:0", "exch", "inferred", "change_of") in tags(doxx([t1, t2], K, [Received("t1:0", "exch")]))


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
    assert {t.entity for t in doxx([tx], K).tags if t.outpoint == "t1:2"} == {"exch", "boss"}


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
        (([Tx("t", (Coin("a", "A", True),), ())], K), "its outpoint"),
        (([Tx("t", (Coin("a:0", "A", "yes"),), ())], K), "whether it is owned"),  # type: ignore[arg-type]
        (([Tx("t", (Coin("a:0", "A", True, "exch"),), ())], K), "no other entity"),
        (([], K, ["r"]), "sequence of Received"),
        (([], K, [Received("", "x")]), "outpoint and entity"),
        (([], K, (), [Override("a:0", "x", 1)]), "whether it is present"),  # type: ignore[arg-type]
        (([], K, (), [Override("a:0", "x", True), Override("a:0", "x", False)]), "both added and removed"),
        (([Tx("t", (), (mine("u:0", "A"),))], K), "txid:index"),
        (([Tx("t", (), (mine("t:0", "A"), mine("t:0", "A")))], K), "txid:index"),
        (
            ([Tx("t", (), (mine("t:0", "A"),)), Tx("u", (theirs("t:0", "A"),), ())], K),
            "differ between sightings",
        ),
        (([Tx("t:1", (), ())], K), "unique txid"),
    ],
)
def test_invalid_input_raises_t511(args: tuple[object, ...], match: str) -> None:
    with pytest.raises(DoxxError, match=match):
        doxx(*args)  # type: ignore[arg-type]


def test_reuse_and_forward_keep_their_sources_confidence_adr0010_t511() -> None:
    # t2 (mixing) gives B's coin an inferred tag: reuse gives B's other coin the same, inferred, and a
    # non-mixing spend of that coin forwards it, still inferred. A rule never turns a guess certain.
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    t2 = Tx("t2", (mine("t1:0", "A"), theirs("z:0", "Z")), (mine("t2:0", "B"),), mixing=True)
    t3 = Tx("t3", (theirs("g:0", "G"),), (mine("t3:0", "B"),))
    t4 = Tx("t4", (mine("t3:0", "B"),), (mine("t4:0", "C"),))
    result = tags(doxx([t1, t2, t3, t4], K, [Received("t1:0", "exch")]))
    assert ("t3:0", "exch", "inferred", "address_reuse") in result
    assert ("t4:0", "exch", "inferred", "change_of") in result
    # and from a certain source, both are certain
    t5 = Tx("t5", (theirs("h:0", "H"),), (mine("t5:0", "A"),))
    assert ("t5:0", "exch", "certain", "address_reuse") in tags(doxx([t1, t5], K, [Received("t1:0", "exch")]))


def test_a_tag_resting_on_the_cluster_heuristic_stays_marked_as_one_t504() -> None:
    # B is inferred only by the cluster (co-spent with A); forwarding it keeps the heuristic mark, and
    # names the coin and transaction it came from
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    t2 = Tx("t2", (theirs("f:0", "F"),), (mine("t2:0", "B"),))
    t3 = Tx("t3", (mine("x:0", "A"), mine("y:0", "B")), (theirs("t3:0", "Y"),))
    t4 = Tx("t4", (mine("t2:0", "B"),), (mine("t4:0", "C"),))
    result = {t.outpoint: t for t in doxx([t1, t2, t3, t4], K, [Received("t1:0", "exch")]).tags}
    assert (result["t2:0"].reason, result["t2:0"].heuristic, result["t2:0"].txid) == (
        "cluster_backward",
        True,
        "t3",
    )
    assert (result["t4:0"].reason, result["t4:0"].heuristic, result["t4:0"].source) == (
        "change_of",
        True,
        "t2:0",
    )
    assert result["t1:0"].heuristic is False


def test_a_full_result_names_each_tags_transaction_and_source_t504() -> None:
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    t2 = Tx("t2", (mine("t1:0", "A"),), (mine("t2:0", "B"), theirs("t2:1", "X", "boss")))
    assert doxx([t1, t2], K, [Received("t1:0", "exch")]).tags == (
        Tag("t1:0", "boss", "certain", "paid_to", "t2"),
        Tag("t1:0", "exch", "certain", "received_from"),
        Tag("t2:0", "boss", "certain", "change_of", "t2"),
        Tag("t2:0", "exch", "certain", "change_of", "t2", "t1:0"),
    )


def test_facts_the_engine_cant_apply_are_returned_unresolved_t511() -> None:
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    result = doxx(
        [t1],
        K,
        [Received("t9:0", "exch"), Received("t1:0", "shop")],
        [Override("t9:1", "exch", True), Override("t1:0", "shop", True)],
    )
    assert result.tags == ()
    assert result.unresolved == (
        Unresolved("override", "t1:0", "shop", "not_identity"),
        Unresolved("override", "t9:1", "exch", "not_owned"),
        Unresolved("received", "t1:0", "shop", "not_identity"),
        Unresolved("received", "t9:0", "exch", "not_owned"),
    )


def test_a_removed_certain_tag_is_returned_as_suppressed_t511() -> None:
    tx = Tx("t1", (mine("a:0", "A"),), (theirs("t1:0", "X", "exch"),))
    result = doxx([tx], K, overrides=[Override("a:0", "exch", False)])
    assert result.tags == ()
    assert result.suppressed == (Tag("a:0", "exch", "certain", "paid_to", "t1"),)


def test_many_coins_at_one_address_are_handled() -> None:
    txs = [Tx(f"t{n}", (theirs(f"e{n}:0", "E"),), (mine(f"t{n}:0", "A"),)) for n in range(2_000)]
    result = doxx(txs, K, [Received("t0:0", "exch")])
    assert len(result.tags) == 2_000


@settings(max_examples=60, deadline=None)
@given(st.permutations(range(4)), st.booleans())
def test_the_result_doesnt_depend_on_the_order_of_the_input_t504(order: list[int], flip: bool) -> None:
    txs = [
        Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),)),
        Tx("t2", (theirs("f:0", "F"),), (mine("t2:0", "B"), mine("t2:1", "A"))),
        Tx("t3", (mine("x:0", "A"), mine("y:0", "B")), (theirs("t3:0", "Y", "boss"), mine("t3:1", "C"))),
        Tx("t4", (mine("t3:1", "C"), theirs("z:0", "Z")), (mine("t4:0", "D"),), mixing=True),
    ]
    received = [Received("t1:0", "exch"), Received("t2:0", "boss")]
    overrides = [Override("t4:0", "exch", True), Override("t2:1", "boss", False)]
    expected = doxx(txs, K, received, overrides)
    shuffled = [txs[i] for i in order]
    assert (
        doxx(shuffled, K, received[::-1] if flip else received, overrides[::-1] if flip else overrides)
        == expected
    )


def test_a_removal_reports_the_tags_it_kept_off_downstream_t511() -> None:
    # a:0 pays exch: certain on a:0 and on the change t1:1, which t2 spends on to t2:0. Removing
    # (a:0, exch) keeps the change's tags, but its own and nothing else: removing (t1:1, exch) keeps
    # t2:0's off too, and both are reported
    t1 = Tx("t1", (mine("a:0", "A"),), (theirs("t1:0", "X", "exch"), mine("t1:1", "B")))
    t2 = Tx("t2", (mine("t1:1", "B"),), (mine("t2:0", "C"),))
    result = doxx([t1, t2], K, overrides=[Override("t1:1", "exch", False)])
    assert {(t.outpoint, t.entity) for t in result.tags} == {("a:0", "exch")}
    assert {(t.outpoint, t.entity, t.reason) for t in result.suppressed} == {
        ("t1:1", "exch", "change_of"),
        ("t2:0", "exch", "change_of"),
    }


def test_removals_the_engine_cant_apply_are_returned_unresolved_t511() -> None:
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "A"),))
    result = doxx([t1], K, overrides=[Override("t9:0", "exch", False), Override("t1:0", "shop", False)])
    assert result.unresolved == (
        Unresolved("override", "t1:0", "shop", "not_identity"),
        Unresolved("override", "t9:0", "exch", "not_owned"),
    )


def test_the_same_fact_twice_is_reported_unresolved_once() -> None:
    result = doxx([], K, [Received("t9:0", "exch"), Received("t9:0", "exch")])
    assert result.unresolved == (Unresolved("received", "t9:0", "exch", "not_owned"),)


def test_address_reuse_names_the_coin_the_link_came_from_t504() -> None:
    # three coins at A, only t3:0 received from exch: the other two name t3:0 and its transaction,
    # never each other
    txs = [Tx(f"t{n}", (theirs(f"e{n}:0", "E"),), (mine(f"t{n}:0", "A"),)) for n in (1, 2, 3)]
    result = {t.outpoint: t for t in doxx(txs, K, [Received("t3:0", "exch")]).tags}
    assert [(result[o].source, result[o].txid) for o in ("t1:0", "t2:0")] == [("t3:0", "t3"), ("t3:0", "t3")]


def test_a_cluster_tag_never_names_its_own_coin_t504() -> None:
    # t1:0 (at C) is received from exch; t3 co-spends a:0 (at A) with it: a:0's source is t1:0
    t1 = Tx("t1", (theirs("e:0", "E"),), (mine("t1:0", "C"),))
    t3 = Tx("t3", (mine("a:0", "A"), mine("t1:0", "C")), (theirs("t3:0", "Y"),))
    result = {t.outpoint: t for t in doxx([t1, t3], K, [Received("t1:0", "exch")]).tags}
    assert (result["a:0"].reason, result["a:0"].source, result["a:0"].txid) == (
        "cluster_backward",
        "t1:0",
        "t3",
    )


def test_a_long_chain_is_followed_whatever_its_txids_order() -> None:
    # 3,000 spends whose txids sort against the spend order: the spend order carries the tag in one pass
    n = 3_000
    txs = [Tx(f"x{n:05}", (theirs("e:0", "E"),), (mine(f"x{n:05}:0", "A0"),))]
    txs += [
        Tx(f"x{i:05}", (mine(f"x{i + 1:05}:0", f"A{n - i - 1}"),), (mine(f"x{i:05}:0", f"A{n - i}"),))
        for i in range(n - 1, -1, -1)
    ]
    result = doxx(txs, K, [Received(f"x{n:05}:0", "exch")])
    assert ("x00000:0", "exch", "certain", "change_of") in tags(result)


@pytest.mark.parametrize(
    ("txs", "match"),
    [
        ([Tx("t", (mine("a:0", "A"),), ()), Tx("u", (mine("a:0", "A"),), ())], "spent by two"),
        ([Tx("t", (), (mine("t:0", "A"),)), Tx("u", (mine("t:5", "A"),), ())], "lacks"),
        (
            [
                Tx("t", (mine("u:0", "A"),), (mine("t:0", "A"),)),
                Tx("u", (mine("t:0", "A"),), (mine("u:0", "A"),)),
            ],
            "cycle",
        ),
        ([Tx("t", (mine("a:03", "A"),), ())], "its outpoint"),
        ([Tx("t", (mine("a:" + "1" * 11, "A"),), ())], "its outpoint"),
        ([Tx("t", (), (mine("t:1", "A"),))], "each at its index"),
        ([Tx("t", (), (mine("t:1", "A"), mine("t:0", "B")))], "each at its index"),
        ([Tx("t", (mine("a:0", "A"),), (theirs("t:0", "A", "exch"),))], "different owners"),
        ([Tx("t", (mine("a:\u00b2", "A"),), ())], "its outpoint"),
    ],
)
def test_impossible_graphs_raise_t511(txs: list[Tx], match: str) -> None:
    with pytest.raises(DoxxError, match=match):
        doxx(txs, K)


def test_a_coin_with_a_heuristic_reuse_tag_isnt_its_own_cluster_source_t504() -> None:
    # p:0 (at P) is received from exch; tq co-spends it with q:0, so Q's coins are inferred by the
    # cluster, and q:1 forwards that to tf:0 at A. A's other coins a1:0 and a3:0 get it by reuse.
    # tc co-spends a3:0 with b:0, so the cluster {A, B} reaches a1:0, the lowest holder: its tag
    # (reuse) is weaker than the cluster's, which would otherwise name a1:0 itself as the source
    txs = [
        Tx("p", (theirs("e:0", "E"),), (mine("p:0", "P"),)),
        Tx("q", (theirs("e:1", "E"),), (mine("q:0", "Q"), mine("q:1", "Q"))),
        Tx("tq", (mine("p:0", "P"), mine("q:0", "Q")), (theirs("tq:0", "Y"),)),
        Tx("tf", (mine("q:1", "Q"),), (mine("tf:0", "A"),)),
        Tx("a1", (theirs("e:2", "E"),), (mine("a1:0", "A"),)),
        Tx("a3", (theirs("e:3", "E"),), (mine("a3:0", "A"),)),
        Tx("b", (theirs("e:4", "E"),), (mine("b:0", "B"),)),
        Tx("tc", (mine("a3:0", "A"), mine("b:0", "B")), (theirs("tc:0", "Y"),)),
    ]
    result = {t.outpoint: t for t in doxx(txs, K, [Received("p:0", "exch")]).tags}
    assert all(t.source != t.outpoint for t in result.values())
    assert (result["a1:0"].reason, result["a1:0"].source) == ("address_reuse", "tf:0")
    # b:0's hop is tc, where it was co-spent with a3:0
    assert (result["b:0"].reason, result["b:0"].source, result["b:0"].txid) == (
        "cluster_backward",
        "a3:0",
        "tc",
    )


def test_paid_to_needs_the_user_to_spend_in_the_transaction_adr0010() -> None:
    # a withdrawal from exch (exch's inputs, the user's output, exch's change) and someone's batch paying
    # boss and the user: no paid-to link; the withdrawal is a suggested receipt instead
    withdrawal = Tx("w", (theirs("x:0", "X", "exch"),), (mine("w:0", "A"), theirs("w:1", "X", "exch")))
    batch = Tx("b", (theirs("y:0", "Y"),), (theirs("b:0", "Z", "boss"), mine("b:1", "B")))
    result = doxx([withdrawal, batch], K)
    assert result.tags == ()
    assert result.suggested == (Suggestion("w:0", "exch", "w"),)


def test_a_recorded_receipt_isnt_suggested_again_t511() -> None:
    withdrawal = Tx("w", (theirs("x:0", "X", "exch"),), (mine("w:0", "A"),))
    assert doxx([withdrawal], K, [Received("w:0", "exch")]).suggested == ()


def test_a_cluster_tag_names_the_hop_that_links_it_t504() -> None:
    # co-spends C+D (t1), A+B (t2), then B+C (t3); the holder is at A. D's tag names t1 and the coin at
    # C it was co-spent with, C's names t3 and the coin at B, B's names t2 and the coin at A
    txs = [
        Tx("f", (theirs("e:0", "E"),), tuple(mine(f"f:{i}", a) for i, a in enumerate("ABCDBC"))),
        Tx("t1", (mine("f:2", "C"), mine("f:3", "D")), (theirs("t1:0", "Y"),)),
        Tx("t2", (mine("f:0", "A"), mine("f:1", "B")), (theirs("t2:0", "Y"),)),
        Tx("t3", (mine("f:4", "B"), mine("f:5", "C")), (theirs("t3:0", "Y"),)),
    ]
    tags_ = {t.outpoint: t for t in doxx(txs, K, overrides=[Override("f:0", "exch", True)]).tags}
    assert (tags_["f:1"].txid, tags_["f:1"].source) == ("t2", "f:0")
    assert (tags_["f:2"].txid, tags_["f:2"].source) == ("t3", "f:4")
    assert (tags_["f:3"].txid, tags_["f:3"].source) == ("t1", "f:2")


def test_a_removal_that_only_weakens_a_tag_is_reported_t511() -> None:
    # r:0 is received from exch and spent on to t1:0 at C (certain); g:0 is received from exch too and
    # co-spent with g:1, also at C, so C's coins are inferred by the cluster. Removing (r:0, exch)
    # leaves t1:0 only the inferred tag: the certain one it lost is reported
    txs = [
        Tx("r", (theirs("e:0", "E"),), (mine("r:0", "A"),)),
        Tx("g", (theirs("e:1", "E"),), (mine("g:0", "G"), mine("g:1", "C"))),
        Tx("t1", (mine("r:0", "A"),), (mine("t1:0", "C"),)),
        Tx("t2", (mine("g:0", "G"), mine("g:1", "C")), (theirs("t2:0", "Y"),)),
    ]
    received = [Received("r:0", "exch"), Received("g:0", "exch")]
    result = doxx(txs, K, received, [Override("r:0", "exch", False)])
    kept = {(t.outpoint, t.entity): t.confidence for t in result.tags}
    lost = {(t.outpoint, t.entity): t.confidence for t in result.suppressed}
    assert kept[("t1:0", "exch")] == "inferred"
    assert lost[("t1:0", "exch")] == "certain"
