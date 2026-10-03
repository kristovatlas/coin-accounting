"""The JSON-RPC transport (architecture §1 F2; THREAT_MODEL T-201, T-202, T-203, T-205, T-209).

These use a loopback HTTP stub for replies a real node won't produce on demand. The node-facing
behaviour (whitelist refusal, auth, real replies) is tested on regtest in tests/integration.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from coinacct.domain.secret import Secret
from coinacct.rpc import (
    ALLOWED_METHODS,
    RpcAuthError,
    RpcCallError,
    RpcClient,
    RpcForbiddenError,
    RpcMethodNotAllowedError,
    RpcResponseTooLargeError,
    RpcTransportError,
)
from tests.stub_http import Reply, jsonrpc_result, serve

PASSWORD = "stub-password-value"
PARAM = "wpkh(tpubSECRETPARAM)"


def fixed(reply: Reply) -> Callable[[Any], Reply]:
    return lambda body: reply


def client(port: int, **kwargs: Any) -> RpcClient:
    return RpcClient("127.0.0.1", port, "ro-client", Secret(PASSWORD), timeout=5, **kwargs)


def test_a_call_sends_json_rpc_2_with_basic_auth_and_returns_the_result() -> None:
    with serve(lambda body: jsonrpc_result(42)) as stub:
        assert client(stub.port).call("getblockcount") == 42
    request = stub.requests[0]
    assert request.body == {"jsonrpc": "2.0", "id": 1, "method": "getblockcount", "params": []}
    expected = "Basic " + base64.b64encode(f"ro-client:{PASSWORD}".encode()).decode()
    assert request.headers["Authorization"] == expected


def test_request_ids_are_a_plain_counter_t209() -> None:
    with serve(lambda body: jsonrpc_result(0)) as stub:
        c = client(stub.port)
        for _ in range(3):
            c.call("getblockcount")
    assert [r.body["id"] for r in stub.requests] == [1, 2, 3]


def test_fractional_numbers_decode_as_decimal_never_float_t502() -> None:
    reply = {"value": 0.1, "nested": [{"amount": 21000000.00000001}], "count": 3}
    with serve(lambda body: (200, json.dumps({"result": reply, "error": None, "id": 1}).encode())) as stub:
        result = client(stub.port).call("gettxout", ["00" * 32, 0])
    assert result["value"] == Decimal("0.1") and isinstance(result["value"], Decimal)
    assert result["nested"][0]["amount"] == Decimal("21000000.00000001")
    assert result["count"] == 3 and isinstance(result["count"], int)


@pytest.mark.parametrize(
    "method", ["sendrawtransaction", "importdescriptors", "stop", "uptime", "getwalletinfo"]
)
def test_methods_outside_the_allowlist_are_refused_before_sending_t203(method: str) -> None:
    with serve(lambda body: jsonrpc_result(None)) as stub:
        with pytest.raises(RpcMethodNotAllowedError):
            client(stub.port).call(method)
    assert stub.requests == []


def test_the_allowlist_matches_the_threat_model_t203() -> None:
    # THREAT_MODEL T-203 lists these; changing them needs an ADR (ADR 0004).
    assert ALLOWED_METHODS == {
        "getblockchaininfo",
        "getnetworkinfo",
        "getindexinfo",
        "getblockcount",
        "getbestblockhash",
        "getblockhash",
        "getblockheader",
        "getblock",
        "getrawtransaction",
        "gettxout",
        "gettxspendingprevout",
        "scanblocks",
        "getdescriptoractivity",
        "getchaintips",
        "deriveaddresses",
        "getdescriptorinfo",
    }


@pytest.mark.parametrize("host", ["192.0.2.1", "10.0.0.1", "0.0.0.0", "localhost", "example.invalid", "::"])
def test_non_loopback_endpoints_are_refused_t202(host: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        RpcClient(host, 8332, "u", Secret("p"))


@pytest.mark.parametrize(
    ("reply", "error", "message"),
    [
        ((401, b""), RpcAuthError, "HTTP 401"),
        ((403, b""), RpcForbiddenError, "rpcwhitelist"),
        ((500, b"<html>oops</html>"), RpcTransportError, "isn't JSON"),
        ((200, b'{"result": NaN, "id": 1}'), RpcTransportError, "isn't JSON"),
        ((200, b"[1, 2]"), RpcTransportError, "isn't JSON-RPC"),
        ((200, b'{"id": 1}'), RpcTransportError, "isn't JSON-RPC"),
        ((200, b'{"error": "text", "id": 1}'), RpcTransportError, "malformed error"),
        ((502, b'{"result": 1, "id": 1}'), RpcTransportError, "HTTP 502"),
    ],
)
def test_bad_replies_raise_typed_errors(reply: Reply, error: type[Exception], message: str) -> None:
    with serve(lambda body: reply) as stub:
        with pytest.raises(error, match=message):
            client(stub.port).call("getblockhash", [PARAM])


def test_a_node_errors_text_is_kept_apart_from_its_string_form_t403() -> None:
    body = {"error": {"code": -5, "message": "wpkh(): key 'tpubSECRETPARAM' is not valid"}, "id": 1}
    with serve(lambda b: (200, json.dumps(body).encode())) as stub:
        with pytest.raises(RpcCallError) as e:
            client(stub.port).call("getdescriptorinfo", [PARAM])
    assert str(e.value) == "getdescriptorinfo: node error -5"
    assert "SECRETPARAM" not in repr(e.value)
    assert e.value.node_message == "wpkh(): key 'tpubSECRETPARAM' is not valid"  # for branching only


def test_a_node_error_carries_its_code_and_message() -> None:
    body = {"jsonrpc": "2.0", "error": {"code": -8, "message": "Block height out of range"}, "id": 1}
    with serve(lambda b: (200, json.dumps(body).encode())) as stub:
        with pytest.raises(RpcCallError) as e:
            client(stub.port).call("getblockhash", [10**9])
    assert (e.value.code, e.value.node_message, e.value.method) == (
        -8,
        "Block height out of range",
        "getblockhash",
    )


def test_errors_never_contain_the_password_or_the_parameters_t201_t403() -> None:
    replies: list[Reply] = [
        (401, b""),
        (403, b""),
        (500, b"x"),
        (200, b'{"error": {"code": -5, "message": "No such tx"}, "id": 1}'),
        # Core quotes the offending parameter in many errors; these are its real formats (31.1):
        (200, b'{"error": {"code": -5, "message": "wpkh(): key \'tpubSECRETPARAM\' is not valid"}, "id": 1}'),
        (
            200,
            b'{"error": {"code": -8, '
            b'"message": "parameter 1 must be of length 64 (not 11, for \'SECRETPARAM\')"}}',
        ),
    ]
    for reply in replies:
        with serve(fixed(reply)) as stub:
            c = client(stub.port)
            with pytest.raises(Exception) as e:
                c.call("getdescriptoractivity", [[], [PARAM], False])
        text = f"{e.value} {e.value!r} {c!r}"
        assert PASSWORD not in text
        assert "SECRETPARAM" not in text


def test_a_reply_over_the_size_cap_is_refused_t205() -> None:
    big = jsonrpc_result("x" * 2048)
    with serve(lambda body: big) as stub:
        with pytest.raises(RpcResponseTooLargeError, match="larger than 1024"):
            client(stub.port, max_response_bytes=1024).call("getblock", ["00" * 32])
        assert client(stub.port, max_response_bytes=len(big[1])).call("getblock", ["00" * 32]) == "x" * 2048


def test_an_unreachable_node_is_a_transport_error() -> None:
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    # Nothing listens on the port any more.
    with pytest.raises(RpcTransportError, match="can't reach the node"):
        client(port).call("getblockcount")


def test_non_json_parameters_are_refused_before_sending() -> None:
    with serve(lambda body: jsonrpc_result(None)) as stub:
        with pytest.raises(TypeError, match="Decimal"):
            client(stub.port).call("getblockhash", [Decimal("1.5")])
    assert stub.requests == []


@pytest.mark.parametrize(
    ("reply", "refused"),
    [
        ((403, b""), True),
        (jsonrpc_result(1234), False),
        ((200, b'{"error": {"code": -32601, "message": "Method not found"}, "id": 1}'), False),
    ],
)
def test_the_canary_is_refused_only_on_http_403_t203(reply: Reply, refused: bool) -> None:
    with serve(lambda body: reply) as stub:
        assert client(stub.port).canary_refused() is refused
    assert stub.requests[0].body["method"] == "uptime"


def test_a_canary_with_bad_credentials_raises_rather_than_passing() -> None:
    with serve(lambda body: (401, b"")) as stub:
        with pytest.raises(RpcAuthError):
            client(stub.port).canary_refused()


def test_concurrent_calls_are_capped() -> None:
    in_flight, peak = 0, 0
    cond = threading.Condition()
    release = threading.Event()

    def respond(body: Any) -> Reply:
        nonlocal in_flight, peak
        with cond:
            in_flight += 1
            peak = max(peak, in_flight)
            cond.notify_all()
        release.wait(timeout=10)
        with cond:
            in_flight -= 1
        return jsonrpc_result(0)

    with serve(respond) as stub:
        c = client(stub.port, max_concurrency=2)
        threads = [threading.Thread(target=c.call, args=("getblockcount",)) for _ in range(4)]
        for t in threads:
            t.start()
        with cond:
            assert cond.wait_for(lambda: in_flight == 2, timeout=10)
            # The other two are held by the client, so the stub never sees a third request.
            assert not cond.wait_for(lambda: in_flight > 2, timeout=0.5)
        release.set()
        for t in threads:
            t.join(timeout=10)
    assert peak == 2
    assert len(stub.requests) == 4
