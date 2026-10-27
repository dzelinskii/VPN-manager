"""Тесты формата AWG-конфига, отдаваемого клиенту AmneziaVPN.

Формат `last_config` должен совпадать с тем, что генерирует сам AmneziaVPN:
начиная с версии клиента 5.0.0 конфиг проходит через типизированную модель
`AwgClientConfig`, где `port` объявлен как int. Строковый `port` там читается
как 0 и затем полностью выпадает из конфига, после чего клиент падает на
разборе endpoint'а. Поэтому тип полей проверяем явно.
"""

from __future__ import annotations

import base64
import json
import zlib
from unittest.mock import MagicMock, patch

import pytest

FERNET_KEY = "SpWH-ifTebQwpAlasE5SvZsgUwi0onGmILmSrm7G1BQ="

OBFUSCATION = {
    "Jc": "5", "Jmin": "20", "Jmax": "80",
    "S1": "40", "S2": "60", "S3": "30", "S4": "70",
    "H1": "1234567-2000000", "H2": "2000001-3000000",
    "H3": "3000001-4000000", "H4": "4000001-5000000",
}

# Сервер, установленный в формате 3.1: S1-S4/H1-H4 фиксированы, есть HP-ключ.
OBFUSCATION_V3 = {
    "Jc": "5", "Jmin": "10", "Jmax": "50",
    "S1": "12", "S2": "12", "S3": "12", "S4": "12",
    "H1": "1", "H2": "2", "H3": "3", "H4": "4",
    "RandomTrailers": "on", "DisableCookies": "on",
    "HeaderProtectionKey": "hEaDeRpRoTeCtIoNkEy1234567890abcdefghijklmnop=",
}

# Новые клиентские поля 3.1 (формат template.conf из amnezia-client).
V3_CLIENT_FIELDS = {
    "HeaderProtectionKey": OBFUSCATION_V3["HeaderProtectionKey"],
    "ContentPaddingAddition": "10-100",
    "RekeyAfterTime": "100-120",
    "RekeyTimeout": "3-7",
    "RejectAfterTime": "150-180",
    "KeepaliveTimeout": "5-15",
    "MaxHandshakeAttempts": "15-20",
    "RandomTrailers": "on",
    "DisableCookies": "on",
}


def _make_settings_mock():
    settings = MagicMock()
    settings.ENCRYPTION_KEY = FERNET_KEY
    return settings


def _decode_vpn_uri(uri: str) -> dict:
    """Раскодировать vpn:// обратно в JSON (обратна _encode_vpn_uri)."""
    b64 = uri.removeprefix("vpn://")
    padded = b64 + "=" * (-len(b64) % 4)
    raw = base64.urlsafe_b64decode(padded)
    return json.loads(zlib.decompress(raw[4:]).decode("utf-8"))


def _build_provider(obfuscation: dict | None = None):
    from app.services.vpn_providers.amnezia_awg import AmneziaAWGProvider

    awg = MagicMock()
    awg.port = 51820
    awg.obfuscation = dict(obfuscation if obfuscation is not None else OBFUSCATION)
    awg.subnet_ip = "10.8.0.0"
    awg.subnet_cidr = 24
    awg.server_public_key = "sErVeRpUbLiCkEy1234567890abcdefghijklmn="

    server = MagicMock()
    server.ip_address = "203.0.113.10"
    server.name = "test-server"
    server.ssh_user = "root"
    server.awg_service = awg

    connection = MagicMock()
    connection.private_key = "cLiEnTpRiVaTeKeY1234567890abcdefghijklm="
    connection.public_key = "cLiEnTpUbLiCkEy1234567890abcdefghijklmn="
    connection.client_ip = "10.8.0.2"
    connection.psk = "pReShArEdKeY1234567890abcdefghijklmnopq="
    connection.subscription.name = "test-sub"

    return AmneziaAWGProvider(server), MagicMock(), connection


@pytest.mark.asyncio
async def test_last_config_port_is_int():
    """`port` внутри last_config обязан быть числом, а не строкой.

    Клиент 5.0.0+ читает его как int; строка превращается в 0 и ключ
    выбрасывается при сериализации, что рвёт подключение.
    """
    with patch("app.config.get_settings", return_value=_make_settings_mock()), \
         patch("app.database._get_engine", return_value=MagicMock()), \
         patch("app.database._get_session_factory", return_value=MagicMock()):
        provider, inbound, connection = _build_provider()
        config = await provider.get_client_config(inbound, connection)

    server_config = _decode_vpn_uri(config["vpn_uri"])
    awg_container = server_config["containers"][0]["awg"]
    last_config = json.loads(awg_container["last_config"])

    assert isinstance(last_config["port"], int), (
        f"last_config.port должен быть int, получен {type(last_config['port']).__name__}"
    )
    assert last_config["port"] == 51820


@pytest.mark.asyncio
async def test_container_port_stays_string():
    """Порт контейнера (уровень awg) клиент читает как строку — тип менять нельзя."""
    with patch("app.config.get_settings", return_value=_make_settings_mock()), \
         patch("app.database._get_engine", return_value=MagicMock()), \
         patch("app.database._get_session_factory", return_value=MagicMock()):
        provider, inbound, connection = _build_provider()
        config = await provider.get_client_config(inbound, connection)

    awg_container = _decode_vpn_uri(config["vpn_uri"])["containers"][0]["awg"]

    assert isinstance(awg_container["port"], str)
    assert awg_container["port"] == "51820"


@pytest.mark.asyncio
async def test_empty_special_junk_params_are_omitted():
    """Пустые I2-I5 в конфиг не попадают — клиент их и сам выбрасывает."""
    with patch("app.config.get_settings", return_value=_make_settings_mock()), \
         patch("app.database._get_engine", return_value=MagicMock()), \
         patch("app.database._get_session_factory", return_value=MagicMock()):
        provider, inbound, connection = _build_provider()
        config = await provider.get_client_config(inbound, connection)

    last_config = json.loads(
        _decode_vpn_uri(config["vpn_uri"])["containers"][0]["awg"]["last_config"]
    )

    for key in ("I2", "I3", "I4", "I5"):
        assert key not in last_config, f"пустой {key} не должен попадать в конфиг"
    assert last_config["I1"], "I1 должен присутствовать"


@pytest.mark.asyncio
async def test_obfuscation_params_present_as_strings():
    """J/S/H-параметры клиент читает строками и требует их наличия."""
    with patch("app.config.get_settings", return_value=_make_settings_mock()), \
         patch("app.database._get_engine", return_value=MagicMock()), \
         patch("app.database._get_session_factory", return_value=MagicMock()):
        provider, inbound, connection = _build_provider()
        config = await provider.get_client_config(inbound, connection)

    last_config = json.loads(
        _decode_vpn_uri(config["vpn_uri"])["containers"][0]["awg"]["last_config"]
    )

    # Android-клиент считает обязательными именно эти девять.
    for key in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"):
        assert isinstance(last_config[key], str)
        assert last_config[key] == OBFUSCATION[key]


def test_generate_obfuscation_params_v3_defaults():
    """3.1: S1-S4=12 (требование HeaderProtectionKey), H1-H4=1..4, тумблеры on."""
    from app.services.installers.awg_installer import generate_obfuscation_params

    obf = generate_obfuscation_params()

    assert obf["S1"] == obf["S2"] == obf["S3"] == obf["S4"] == "12"
    assert [obf["H1"], obf["H2"], obf["H3"], obf["H4"]] == ["1", "2", "3", "4"]
    assert obf["RandomTrailers"] == "on"
    assert obf["DisableCookies"] == "on"
    assert obf["Jmin"] == "10"
    assert obf["Jmax"] == "50"
    assert 4 <= int(obf["Jc"]) <= 6
    # HP-ключ генерируется на сервере при установке, в шаблоне его нет.
    assert "HeaderProtectionKey" not in obf


@pytest.mark.asyncio
async def test_v3_config_contains_31_fields():
    """Для сервера 3.1 клиентский конфиг обязан нести HP-ключ и новые поля."""
    with patch("app.config.get_settings", return_value=_make_settings_mock()), \
         patch("app.database._get_engine", return_value=MagicMock()), \
         patch("app.database._get_session_factory", return_value=MagicMock()):
        provider, inbound, connection = _build_provider(obfuscation=OBFUSCATION_V3)
        config = await provider.get_client_config(inbound, connection)

    for key, val in V3_CLIENT_FIELDS.items():
        line = f"{key} = {val}"
        assert line in config["config_data"], f"в .conf нет строки '{line}'"

    last_config = json.loads(
        _decode_vpn_uri(config["vpn_uri"])["containers"][0]["awg"]["last_config"]
    )
    for key, val in V3_CLIENT_FIELDS.items():
        assert isinstance(last_config.get(key), str), f"{key} должен быть строкой в last_config"
        assert last_config[key] == val

    # 2.x-поля при этом остаются (клиент 3.1 читает и их).
    for key in ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4", "I1"):
        assert isinstance(last_config[key], str)


@pytest.mark.asyncio
async def test_legacy_config_has_no_v3_fields():
    """Сервер 2.x (без HeaderProtectionKey) — новые поля в конфиг не попадают."""
    with patch("app.config.get_settings", return_value=_make_settings_mock()), \
         patch("app.database._get_engine", return_value=MagicMock()), \
         patch("app.database._get_session_factory", return_value=MagicMock()):
        provider, inbound, connection = _build_provider(obfuscation=dict(OBFUSCATION))
        config = await provider.get_client_config(inbound, connection)

    assert "HeaderProtectionKey" not in config["config_data"]
    assert "ContentPaddingAddition" not in config["config_data"]
    assert "RandomTrailers" not in config["config_data"]

    last_config = json.loads(
        _decode_vpn_uri(config["vpn_uri"])["containers"][0]["awg"]["last_config"]
    )
    for key in V3_CLIENT_FIELDS:
        assert key not in last_config, f"{key} не должен попадать в конфиг сервера 2.x"
