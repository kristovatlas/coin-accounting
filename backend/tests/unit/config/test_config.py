"""config.toml parsing (architecture §6; THREAT_MODEL T-201, T-202)."""

from __future__ import annotations

import pytest

from coinacct.config import ConfigError, parse

PASSWORD = "correct-horse-battery-staple"


def config_text(
    host: str = '"127.0.0.1"',
    port: str = "18443",
    user: str = '"ro-client"',
    password: str = f'"{PASSWORD}"',
    extra: str = "",
) -> str:
    return f"[rpc]\nhost = {host}\nport = {port}\nuser = {user}\npassword = {password}\n{extra}"


def test_a_complete_loopback_config_parses() -> None:
    config = parse(config_text())
    assert (config.rpc.host, config.rpc.port, config.rpc.user) == ("127.0.0.1", 18443, "ro-client")
    assert config.rpc.password.reveal() == PASSWORD


def test_ipv6_loopback_is_accepted_t202() -> None:
    assert parse(config_text(host='"::1"')).rpc.host == "::1"


def test_the_password_never_appears_in_the_parsed_config_repr_t201() -> None:
    config = parse(config_text())
    for text in (repr(config), str(config), f"{config}", repr(config.rpc), f"{config.rpc.password}"):
        assert PASSWORD not in text
        assert "<redacted>" in text


@pytest.mark.parametrize("host", ['"192.0.2.10"', '"10.0.0.2"', '"0.0.0.0"', '"::"', '"2001:db8::1"'])
def test_non_loopback_addresses_are_refused_without_override_t202(host: str) -> None:
    with pytest.raises(ConfigError, match="not a loopback address"):
        parse(config_text(host=host))


@pytest.mark.parametrize(
    "host", ['"localhost"', '"node.example.invalid"', '"127.0.0.1.example.invalid"', '""']
)
def test_host_names_are_refused_even_localhost_t202(host: str) -> None:
    with pytest.raises(ConfigError, match="literal loopback address"):
        parse(config_text(host=host))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("port", "0", "rpc.port"),
        ("port", "65536", "rpc.port"),
        ("port", "true", "rpc.port"),
        ("port", '"8332"', "rpc.port"),
        ("user", '""', "rpc.user"),
        ("user", '"a:b"', "rpc.user"),
        ("user", '"a\\nb"', "rpc.user"),
        ("password", '""', "rpc.password"),
        ("password", '"a\\u0000b"', "rpc.password"),
        ("host", "1", "rpc.host"),
    ],
)
def test_invalid_values_are_errors_not_defaults(field: str, value: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message) as e:
        parse(config_text(**{field: value}))
    assert PASSWORD not in str(e.value)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (config_text(extra="timeout = 5\n"), "unknown keys: timeout"),
        (config_text() + "\n[wallet]\nname = 'x'\n", "unknown sections or keys: wallet"),
        ("[rpc]\nhost = '127.0.0.1'\nport = 1\nuser = 'u'\n", "missing: password"),
        ("rpc = 1\n", "needs an \\[rpc\\] section"),
        ("", "needs an \\[rpc\\] section"),
    ],
)
def test_unknown_or_missing_keys_fail_closed(text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(text)


def test_a_toml_syntax_error_doesnt_echo_the_password_t201() -> None:
    broken = config_text(password=f'"{PASSWORD}')  # unterminated string
    with pytest.raises(ConfigError, match="not valid TOML") as e:
        parse(broken)
    assert PASSWORD not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__
