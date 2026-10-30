"""Тесты интеропа бота с официальным клиентом Amnezia (общий AWG-контейнер).

Покрывает: парсер живого awg0.conf (включая конфиг контейнера приложения),
маппинг имён контейнеров на тулзы, list_peers + clientsTable + статистику,
wipe-детектор, детект внешних пиров и досылку pending_push.
См. docs/amnezia-awg-parallel-management.md.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

FERNET_KEY = "SpWH-ifTebQwpAlasE5SvZsgUwi0onGmILmSrm7G1BQ="

# Живой awg0.conf контейнера amnezia-awg2, установленного официальным
# приложением (слепок с сервера FR-HIP, analysis/fr-hip/official-install-state.txt).
OFFICIAL_CONF = """[Interface]
PrivateKey = cC3UvV43GrCIRJUU9RiRywnRogj1oXXfuurPI8sBUmk=
Address = 10.8.1.0/24
ListenPort = 42213
Jc = 6
Jmin = 10
Jmax = 50
S1 = 12
S2 = 12
S3 = 12
S4 = 12
H1 = 1
H2 = 2
H3 = 3
H4 = 4
HeaderProtectionKey = NQkb82lQDBq3Qu6VYB+W2Zg4dFwsyF2e2wzR3g8VO1I=
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = on
DisableCookies = on
# I1 = <r 2><b 0x858000010001000000000669636c6f756403636f6d0000010001c00c000100010000105a00044d583737>
[Peer]
PublicKey = cwUhQFLNizgRL+y8AIhQxmntjCSLLza8VHMU8qK0QWc=
PresharedKey = HP7oPkv9PAbgB68UDmHrcAt1r3YrY9jRcChfI58bz+4=
AllowedIPs = 10.8.1.1/32

[Peer]
PublicKey = pGJ0b1Q1As3Dn4NEHtjpp7eHzUSoNwaRzrQ+WbR/oiM=
PresharedKey = HP7oPkv9PAbgB68UDmHrcAt1r3YrY9jRcChfI58bz+4=
AllowedIPs = 10.8.1.2/32
"""

LEGACY_V2_CONF = """[Interface]
PrivateKey = aaaa
Address = 10.8.0.1/24
ListenPort = 51820
Jc = 4
Jmin = 10
Jmax = 50
S1 = 15
S2 = 15
S3 = 16
S4 = 16
H1 = 1
H2 = 2
H3 = 3
H4 = 4
"""


def _make_settings_mock():
    settings = MagicMock()
    settings.ENCRYPTION_KEY = FERNET_KEY
    return settings


# ── parse_awg_conf ─────────────────────────────────────────────────────────


def test_parse_official_awg2_conf():
    """Парсер понимает конфиг контейнера приложения: 3.1 + закомментированный I1."""
    from app.services.installers.awg_installer import parse_awg_conf

    parsed = parse_awg_conf(OFFICIAL_CONF)

    assert parsed["port"] == 42213
    assert parsed["subnet_ip"] == "10.8.1.0"
    assert parsed["subnet_cidr"] == 24
    assert parsed["server_private_key"] == "cC3UvV43GrCIRJUU9RiRywnRogj1oXXfuurPI8sBUmk="
    assert parsed["protocol_version"] == "3.1"

    obf = parsed["obfuscation"]
    assert obf["HeaderProtectionKey"] == "NQkb82lQDBq3Qu6VYB+W2Zg4dFwsyF2e2wzR3g8VO1I="
    assert obf["Jc"] == "6"
    assert obf["RandomTrailers"] == "on"
    assert obf["RekeyAfterTime"] == "100-120"
    # I1 закомментирован (так пишет официальный клиент) — всё равно читаем.
    assert obf["I1"].startswith("<r 2><b 0x85")
    assert obf["I2"] == ""


def test_parse_legacy_v2_conf():
    """Нет HP-ключа, но есть S3/S4 → версия 2 (legacy-контейнер amnezia-awg)."""
    from app.services.installers.awg_installer import parse_awg_conf

    parsed = parse_awg_conf(LEGACY_V2_CONF)
    assert parsed["protocol_version"] == "2"
    assert "HeaderProtectionKey" not in parsed["obfuscation"]
    assert parsed["port"] == 51820


# ── Provider: маппинг контейнеров ──────────────────────────────────────────


def _build_provider(container_name: str | None):
    from app.services.vpn_providers.amnezia_awg import AmneziaAWGProvider

    awg = MagicMock()
    awg.container_name = container_name or "vpnbot-awg"
    server = MagicMock()
    server.ssh_user = "root"
    server.awg_service = awg
    return AmneziaAWGProvider(server)


def test_provider_default_container():
    provider = _build_provider(None)
    assert provider.container_name == "vpnbot-awg"
    assert provider.config_path == "/opt/amnezia/awg/awg0.conf"
    assert provider.interface_name == "awg0"
    assert provider._bin == "awg"


def test_provider_official_awg2_container():
    provider = _build_provider("amnezia-awg2")
    assert provider.container_name == "amnezia-awg2"
    # Тот же конфиг-путь и тулзы, что у контейнера бота.
    assert provider.config_path == "/opt/amnezia/awg/awg0.conf"
    assert provider._bin == "awg"


def test_provider_legacy_official_container():
    provider = _build_provider("amnezia-awg")
    assert provider.config_path == "/opt/amnezia/awg/wg0.conf"
    assert provider.interface_name == "wg0"
    assert provider._bin == "wg"
    assert provider._quick_bin == "wg-quick"


# ── Provider: list_peers / clientsTable / статистика ───────────────────────


def _provider_with_ssh(ssh_outputs: dict[str, str]):
    """Провайдер, чей _cmd отвечает по подстроке-маркеру команды."""
    provider = _build_provider("amnezia-awg2")

    async def fake_cmd(cmd: str, input_data: str | None = None) -> str:
        for marker, out in ssh_outputs.items():
            if marker in cmd:
                return out
        raise AssertionError(f"unexpected command: {cmd}")

    provider._cmd = fake_cmd
    return provider


@pytest.mark.asyncio
async def test_list_peers_merges_config_table_stats():
    dump = (
        "kPrivAAAA\t51820\t0x00\n"
        "cwUhQFLNizgRL+y8AIhQxmntjCSLLza8VHMU8qK0QWc=\tHP7oPkv9PAbgB68UDmHrcAt1r3YrY9jRcChfI58bz+4=\t"
        "(none)\t10.8.1.1/32\t1700000000\t1024\t2048\toff\n"
        "pGJ0b1Q1As3Dn4NEHtjpp7eHzUSoNwaRzrQ+WbR/oiM=\tHP7oPkv9PAbgB68UDmHrcAt1r3YrY9jRcChfI58bz+4=\t"
        "1.2.3.4:5\t10.8.1.2/32\t0\t0\t0\toff\n"
    )
    clients_table = json.dumps([
        {"clientId": "cwUhQFLNizgRL+y8AIhQxmntjCSLLza8VHMU8qK0QWc=",
         "userData": {"clientName": "Телефон админа"}},
    ])
    provider = _provider_with_ssh({
        "cat /opt/amnezia/awg/awg0.conf": OFFICIAL_CONF,
        "cat /opt/amnezia/awg/clientsTable": clients_table,
        "awg show awg0 dump": dump,
    })

    peers = await provider.list_peers()

    assert len(peers) == 2
    first = peers[0]
    assert first["public_key"] == "cwUhQFLNizgRL+y8AIhQxmntjCSLLza8VHMU8qK0QWc="
    assert first["client_ip"] == "10.8.1.1"
    assert first["name"] == "Телефон админа"
    assert first["rx"] == 1024
    assert first["tx"] == 2048
    assert first["last_handshake"] == 1700000000
    # Второй пир без записи в clientsTable — имя пустое, статистики нет.
    assert peers[1]["name"] == ""
    assert peers[1]["last_handshake"] == 0


@pytest.mark.asyncio
async def test_peers_stats_filters_interface_row():
    provider = _build_provider("vpnbot-awg")
    peer_key = "b" * 43 + "="
    dump = (
        "c" * 43 + "=\t51820\t0x00\n"
        f"{peer_key}\tpsK=\t(none)\t10.8.0.2/32\t1700000001\t10\t20\toff\n"
    )
    provider._cmd = AsyncMock(return_value=dump)

    stats = await provider._get_peers_stats()
    assert stats == {peer_key: {"last_handshake": 1700000001, "rx": 10, "tx": 20}}


@pytest.mark.asyncio
async def test_clients_table_upsert_and_remove():
    provider = _build_provider("amnezia-awg2")
    state = {"table": json.dumps([
        {"clientId": "kPublOld=", "userData": {"clientName": "Старый"}},
    ])}

    async def fake_cmd(cmd: str, input_data: str | None = None) -> str:
        if input_data is not None:
            state["table"] = input_data
            return ""
        if "clientsTable" in cmd:
            return state["table"]
        raise AssertionError(f"unexpected command: {cmd}")

    provider._cmd = fake_cmd

    await provider.upsert_clients_table_entry("kPublNew=", "Мой клиент")
    written = json.loads(state["table"])
    assert [e["clientId"] for e in written] == ["kPublOld=", "kPublNew="]
    assert written[1]["userData"]["clientName"] == "Мой клиент"

    await provider.remove_clients_table_entry("kPublOld=")
    written = json.loads(state["table"])
    assert [e["clientId"] for e in written] == ["kPublNew="]


@pytest.mark.asyncio
async def test_get_client_traffic_contract():
    """Контракт BaseVPNProvider: upload/download/total (+ last_handshake)."""
    provider = _build_provider("vpnbot-awg")
    peer_key = "d" * 43 + "="
    dump = (
        "e" * 43 + "=\t51820\t0x00\n"
        f"{peer_key}\tpsK=\t(none)\t10.8.0.2/32\t1700000000\t100\t50\toff\n"
    )
    provider._cmd = AsyncMock(return_value=dump)
    connection = MagicMock()
    connection.public_key = peer_key

    result = await provider.get_client_traffic(MagicMock(), connection)

    assert result == {
        "upload": 50,     # tx сервера — отправлено клиенту вниз
        "download": 100,  # rx сервера — принято от клиента
        "total": 150,
        "last_handshake": 1700000000,
    }


# ── discover_existing через docker exec ─────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_existing_reads_container_fs():
    """Discover работает без хостового volume — читает конфиг контейнера приложения."""
    with patch("app.config.get_settings", return_value=_make_settings_mock()), \
         patch("app.database._get_engine", return_value=MagicMock()), \
         patch("app.database._get_session_factory", return_value=MagicMock()):
        from app.services.installers.awg_installer import AWGInstaller

        installer = AWGInstaller(MagicMock())

        async def fake_cmd(cmd: str, input_data: str | None = None) -> str:
            if "cat /opt/amnezia/awg/awg0.conf" in cmd:
                return OFFICIAL_CONF
            if "wireguard_server_public_key.key" in cmd:
                return "6z3uiQ1+sgWThOE4WhS5CsW7h6HD13Lmn3sbDYuCtRQ="
            if "label=vpnbot.service" in cmd:
                return ""  # метки бота нет — контейнер приложения
            raise AssertionError(f"unexpected command: {cmd}")

        installer._cmd = fake_cmd

        result = await installer.discover_existing("amnezia-awg2")

    assert result["port"] == 42213
    assert result["subnet_ip"] == "10.8.1.0"
    assert result["server_public_key"] == "6z3uiQ1+sgWThOE4WhS5CsW7h6HD13Lmn3sbDYuCtRQ="
    assert result["server_private_key"] == "cC3UvV43GrCIRJUU9RiRywnRogj1oXXfuurPI8sBUmk="
    assert result["container_name"] == "amnezia-awg2"
    assert result["is_external"] is True
    assert result["protocol_version"] == "3.1"
    assert result["obfuscation"]["HeaderProtectionKey"] == "NQkb82lQDBq3Qu6VYB+W2Zg4dFwsyF2e2wzR3g8VO1I="


@pytest.mark.asyncio
async def test_discover_existing_bot_label_beats_name():
    """Контейнер бота с именем amnezia-awg2 — НЕ внешний: метка vpnbot.service=awg.

    Имя не маркер: бот может ставить контейнер с «официальным» именем, чтобы
    приложение его подобрало (reverse-interop).
    """
    with patch("app.config.get_settings", return_value=_make_settings_mock()), \
         patch("app.database._get_engine", return_value=MagicMock()), \
         patch("app.database._get_session_factory", return_value=MagicMock()):
        from app.services.installers.awg_installer import AWGInstaller

        installer = AWGInstaller(MagicMock())

        async def fake_cmd(cmd: str, input_data: str | None = None) -> str:
            if "cat /opt/amnezia/awg/awg0.conf" in cmd:
                return OFFICIAL_CONF
            if "wireguard_server_public_key.key" in cmd:
                return "6z3uiQ1+sgWThOE4WhS5CsW7h6HD13Lmn3sbDYuCtRQ="
            if "label=vpnbot.service" in cmd:
                return "amnezia-awg2\n"  # контейнер с меткой бота
            raise AssertionError(f"unexpected command: {cmd}")

        installer._cmd = fake_cmd
        result = await installer.discover_existing("amnezia-awg2")

    assert result["is_external"] is False


@pytest.mark.asyncio
async def test_list_awg_containers_label_logic():
    """list_awg_containers: external по метке, приоритет своего контейнера первым."""
    with patch("app.config.get_settings", return_value=_make_settings_mock()), \
         patch("app.database._get_engine", return_value=MagicMock()), \
         patch("app.database._get_session_factory", return_value=MagicMock()):
        from app.services.installers.awg_installer import AWGInstaller

        installer = AWGInstaller(MagicMock())

        async def fake_cmd(cmd: str, input_data: str | None = None) -> str:
            if cmd.startswith("docker ps -a --format"):
                return "vpnbot-xui\nvpnbot-awg\namnezia-awg2\nsomething-else\n"
            if "label=vpnbot.service" in cmd:
                return "vpnbot-awg\n"  # только свой контейнер с меткой
            raise AssertionError(f"unexpected command: {cmd}")

        installer._cmd = fake_cmd
        containers = await installer.list_awg_containers()

    assert containers == [
        {"name": "vpnbot-awg", "external": False},
        {"name": "amnezia-awg2", "external": True},
    ]


@pytest.mark.asyncio
async def test_discover_existing_derives_pubkey():
    """Файла pubkey нет — выводим публичный ключ из приватного (тулзы контейнера)."""
    with patch("app.config.get_settings", return_value=_make_settings_mock()), \
         patch("app.database._get_engine", return_value=MagicMock()), \
         patch("app.database._get_session_factory", return_value=MagicMock()):
        from app.services.installers.awg_installer import AWGInstaller

        installer = AWGInstaller(MagicMock())

        async def fake_cmd(cmd: str, input_data: str | None = None) -> str:
            if "cat /opt/amnezia/awg/awg0.conf" in cmd:
                return OFFICIAL_CONF
            if "wireguard_server_public_key.key" in cmd:
                raise RuntimeError("no such file")
            if "label=vpnbot.service" in cmd:
                return ""
            if "awg pubkey" in cmd:
                return "dErIvEdPuBkEy1234567890abcdefghijklmn="
            raise AssertionError(f"unexpected command: {cmd}")

        installer._cmd = fake_cmd
        result = await installer.discover_existing("amnezia-awg2")

    assert result["server_public_key"] == "dErIvEdPuBkEy1234567890abcdefghijklmn="


# ── Wipe-детектор ───────────────────────────────────────────────────────────


def _awg_service_mock(**kwargs):
    awg = MagicMock()
    awg.server_public_key = "kEyInDbAAAA="
    awg.obfuscation = {"HeaderProtectionKey": "hPkInDbAAAA=", "Jc": "5"}
    awg.wipe_detected = False
    awg.external_peers_seen = []
    for key, val in kwargs.items():
        setattr(awg, key, val)
    return awg


def _sync_instance():
    from app.services.protocol_sync.awg_sync import AWGProtocolSync

    return AWGProtocolSync()


def _provider_with_live_keys(pub: str | None, hpk: str | None):
    provider = _build_provider("amnezia-awg2")
    provider.get_live_server_keys = AsyncMock(return_value={
        "server_public_key": pub,
        "HeaderProtectionKey": hpk,
    })
    return provider


@pytest.mark.asyncio
async def test_wipe_detector_flags_key_mismatch():
    sync = _sync_instance()
    provider = _provider_with_live_keys("kEyOnServerBBBB=", "hPkInDbAAAA=")
    awg = _awg_service_mock()
    server = MagicMock()
    server.id = 1
    server.name = "srv"

    notified = []
    sync._notify_wipe = AsyncMock(side_effect=lambda *a, **k: notified.append(a))

    wiped = await sync._check_wipe(provider, awg, server, MagicMock())

    assert wiped is True
    assert awg.wipe_detected is True
    assert notified, "админы должны получить уведомление о wipe"


@pytest.mark.asyncio
async def test_wipe_detector_hpk_mismatch_only():
    """Публичный ключ совпал, но HPK сменился — тоже wipe (тихая смерть хендшейков)."""
    sync = _sync_instance()
    provider = _provider_with_live_keys("kEyInDbAAAA=", "hPkOnServerCCCC=")
    awg = _awg_service_mock()

    wiped = await sync._check_wipe(provider, awg, MagicMock(), MagicMock())

    assert wiped is True
    assert awg.wipe_detected is True


@pytest.mark.asyncio
async def test_wipe_detector_ok_when_keys_match():
    sync = _sync_instance()
    provider = _provider_with_live_keys("kEyInDbAAAA=", "hPkInDbAAAA=")
    awg = _awg_service_mock()

    wiped = await sync._check_wipe(provider, awg, MagicMock(), MagicMock())

    assert wiped is False
    assert awg.wipe_detected is False


@pytest.mark.asyncio
async def test_wipe_detector_ssh_failure_is_not_wipe():
    sync = _sync_instance()
    provider = _build_provider("amnezia-awg2")
    provider.get_live_server_keys = AsyncMock(side_effect=RuntimeError("ssh down"))
    awg = _awg_service_mock()

    wiped = await sync._check_wipe(provider, awg, MagicMock(), MagicMock())

    assert wiped is False
    assert awg.wipe_detected is False


@pytest.mark.asyncio
async def test_wipe_detector_empty_db_keys_no_false_positive():
    """Легаси-строка без pubkey в БД не должна давать ложный wipe."""
    sync = _sync_instance()
    provider = _provider_with_live_keys("kEyOnServerBBBB=", None)
    awg = _awg_service_mock(server_public_key=None, obfuscation={})

    wiped = await sync._check_wipe(provider, awg, MagicMock(), MagicMock())

    assert wiped is False


# ── Внешние пиры ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_external_peers_notify_once():
    sync = _sync_instance()
    provider = _build_provider("amnezia-awg2")
    provider.list_peers = AsyncMock(return_value=[
        {"public_key": "kPublExtAAAA=", "client_ip": "10.8.1.9", "name": "Из приложения"},
    ])
    awg = _awg_service_mock()
    server = MagicMock()
    server.id = 7
    server.name = "srv"

    notified = []
    sync._notify_external_peers = AsyncMock(side_effect=lambda *a, **k: notified.append(a))

    from app.database.models import AWGInboundConnection

    own = MagicMock(spec=AWGInboundConnection)
    own.public_key = "kPublOwnBBBB="
    connections = [own]

    await sync._sync_external_peers(provider, awg, server, connections, MagicMock())
    assert len(notified) == 1
    assert awg.external_peers_seen == ["kPublExtAAAA="]

    # Второй цикл без изменений — уведомлений больше нет.
    await sync._sync_external_peers(provider, awg, server, connections, MagicMock())
    assert len(notified) == 1


@pytest.mark.asyncio
async def test_external_peers_detects_gone():
    sync = _sync_instance()
    provider = _build_provider("amnezia-awg2")
    provider.list_peers = AsyncMock(return_value=[])
    awg = _awg_service_mock(external_peers_seen=["kPublExtAAAA="])

    notified = []
    sync._notify_external_peers = AsyncMock(side_effect=lambda *a, **k: notified.append(a))

    await sync._sync_external_peers(provider, awg, MagicMock(), [], MagicMock())

    assert len(notified) == 1
    assert awg.external_peers_seen == []


# ── pending_push досылка ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_push_resolved_when_server_ok():
    """pending_push + сервер уже в нужном состоянии → флаг снимается."""
    from datetime import UTC, datetime

    from app.database.models import AWGInboundConnection

    sync = _sync_instance()
    provider = _build_provider("vpnbot-awg")
    provider._peer_in_config = AsyncMock(return_value=True)
    provider.enable_client = AsyncMock(return_value=True)
    provider.disable_client = AsyncMock(return_value=True)

    conn = MagicMock(spec=AWGInboundConnection)
    conn.sync_status = "pending_push"
    conn.is_enabled = True
    conn.public_key = "kPublCCCC="
    sub = MagicMock()
    sub.is_active = True
    sub.expiry_date = None
    conn.subscription = sub

    await sync._sync_expiry(provider, MagicMock(), [conn], datetime.now(UTC))

    assert conn.sync_status == "synced"
    provider.enable_client.assert_not_awaited()
    provider.disable_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_push_reenables_missing_peer():
    from datetime import UTC, datetime

    from app.database.models import AWGInboundConnection

    sync = _sync_instance()
    provider = _build_provider("vpnbot-awg")
    provider._peer_in_config = AsyncMock(return_value=False)
    provider.enable_client = AsyncMock(return_value=True)

    conn = MagicMock(spec=AWGInboundConnection)
    conn.sync_status = "pending_push"
    conn.is_enabled = False
    conn.public_key = "kPublDDDD="
    sub = MagicMock()
    sub.is_active = True
    sub.expiry_date = None
    conn.subscription = sub

    await sync._sync_expiry(provider, MagicMock(), [conn], datetime.now(UTC))

    provider.enable_client.assert_awaited_once()
    assert conn.is_enabled is True
    assert conn.sync_status == "synced"


@pytest.mark.asyncio
async def test_expiry_disable_failure_marks_pending_push():
    from datetime import UTC, datetime, timedelta

    from app.database.models import AWGInboundConnection

    sync = _sync_instance()
    provider = _build_provider("vpnbot-awg")
    provider.disable_client = AsyncMock(return_value=False)

    conn = MagicMock(spec=AWGInboundConnection)
    conn.sync_status = "synced"
    conn.is_enabled = True
    conn.public_key = "kPublEEEE="
    sub = MagicMock()
    sub.is_active = True
    sub.expiry_date = datetime.now(UTC) - timedelta(days=1)
    conn.subscription = sub

    await sync._sync_expiry(provider, MagicMock(), [conn], datetime.now(UTC))

    assert conn.sync_status == "pending_push"
    assert conn.is_enabled is True


# ── Блокировка выдачи при wipe ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_client_config_blocked_on_wipe():
    with patch("app.config.get_settings", return_value=_make_settings_mock()), \
         patch("app.database._get_engine", return_value=MagicMock()), \
         patch("app.database._get_session_factory", return_value=MagicMock()):
        from app.services.vpn_providers.amnezia_awg import AmneziaAWGProvider

        awg = MagicMock()
        awg.wipe_detected = True
        server = MagicMock()
        server.ssh_user = "root"
        server.awg_service = awg

        provider = AmneziaAWGProvider(server)
        config = await provider.get_client_config(MagicMock(), MagicMock())

    assert config["config_data"] is None
    assert config["error"] == "wipe_detected"
