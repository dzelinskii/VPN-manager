"""AmneziaWG (AWG/AWG2) Provider implementation."""

import base64
import io
import ipaddress
import json
import re
import struct
import uuid
import zlib
from datetime import datetime
from typing import Any

import qrcode
from loguru import logger

from app.database.models import Inbound, InboundConnection, Server, Subscription
from app.services.installers.awg_installer import V3_TIMING_DEFAULTS
from app.services.ssh_service import SSHManager
from app.services.vpn_providers.base import BaseVPNProvider

I1_DEFAULT = "<r 2><b 0x858000010001000000000669636c6f756403636f6d0000010001c00c000100010000105a00044d583737>"

# Тайминги 3.1 общие для сервера и клиента (см. V3_TIMING_DEFAULTS в
# awg_installer). ContentPaddingAddition официальный клиент не выставляет —
# не выставляем и мы.


def _is_v3_obfuscation(obfuscation: dict | None) -> bool:
    """Сервер установлен в формате 3.1 — в obfuscation лежит HeaderProtectionKey."""
    return bool(obfuscation and obfuscation.get("HeaderProtectionKey"))


class AmneziaAWGProvider(BaseVPNProvider):
    """Provider for AmneziaWG via direct SSH and Docker execution.

    Client lifecycle:
    - add_client: generates keys, allocates IP, adds peer to config + kernel
    - enable_client: re-adds peer to config + kernel (keys/IP from DB, same config for user)
    - disable_client: removes peer from kernel only (config entry + IP preserved)
    - remove_client: removes peer from kernel + config, frees IP
    """

    def __init__(self, server: Server) -> None:
        super().__init__(server)
        self.ssh = SSHManager(server)
        self.container_name = "vpnbot-awg"
        self.config_path = "/opt/amnezia/awg/awg0.conf"
        self.interface_name = "awg0"
        self._is_root = (server.ssh_user == "root")

    async def _cmd(self, cmd: str, input_data: str | None = None) -> str:
        """Run command with sudo if needed."""
        if self._is_root:
            full_cmd = cmd
        else:
            full_cmd = f"sudo -n {cmd}"
        return await self.ssh.run_command(full_cmd, input_data=input_data)

    async def _get_server_psk(self) -> str:
        cmd = f"docker exec -i {self.container_name} cat /opt/amnezia/awg/wireguard_psk.key"
        try:
            return await self._cmd(cmd)
        except Exception:
            logger.warning("Не удалось прочитать PSK с сервера, генерирую новый")
            return await self._cmd(f"docker exec -i {self.container_name} awg genpsk")

    async def _get_server_public_key(self) -> str:
        cmd = f"docker exec -i {self.container_name} cat /opt/amnezia/awg/wireguard_server_public_key.key"
        return await self._cmd(cmd)

    async def _find_next_free_ip(self, inbound: Inbound) -> str:
        cmd = f"docker exec -i {self.container_name} cat {self.config_path}"
        config_text = await self._cmd(cmd)

        file_ips = re.findall(r"AllowedIPs\s*=\s*([0-9\.]+)/32", config_text)

        db_ips = []

        from sqlalchemy import select

        from app.database.models import AWGInboundConnection

        if hasattr(self, "_session") and self._session:
            result = await self._session.execute(
                select(AWGInboundConnection).where(AWGInboundConnection.inbound_id == inbound.id)
            )
            connections = result.scalars().all()
        else:
            from app.database import async_session_factory

            async with async_session_factory() as session:
                result = await session.execute(
                    select(AWGInboundConnection).where(AWGInboundConnection.inbound_id == inbound.id)
                )
                connections = result.scalars().all()

        for conn in connections:
            if ip := conn.client_ip:
                db_ips.append(ip)

        all_used_ips = set(file_ips + db_ips)

        if not all_used_ips:
            iface_match = re.search(r"Address\s*=\s*([0-9\.]+)/[0-9]+", config_text)
            base_ip = iface_match.group(1) if iface_match else "10.8.1.1"
            next_ip_obj = ipaddress.IPv4Address(base_ip) + 1
        else:
            ip_objs = sorted([ipaddress.IPv4Address(ip) for ip in all_used_ips])
            next_ip_obj = ip_objs[-1] + 1

        return str(next_ip_obj)

    async def _get_awg_server_params(self) -> dict[str, str]:
        cmd = f"docker exec -i {self.container_name} cat {self.config_path}"
        config_text = await self._cmd(cmd)

        params = {}
        for line in config_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, val = [x.strip() for x in stripped.split("=", 1)]
            params[key] = val

        for key in ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"):
            if key not in params:
                params[key] = "0"

        return params

    @staticmethod
    def _get_i_params() -> dict[str, str]:
        return {
            "I1": I1_DEFAULT,
            "I2": "",
            "I3": "",
            "I4": "",
            "I5": "",
        }

    async def _sync_config(self) -> None:
        sync_cmd = (
            f"docker exec -i {self.container_name} bash -c "
            f"'awg syncconf {self.interface_name} <(awg-quick strip {self.config_path})'"
        )
        await self._cmd(sync_cmd)

    async def _add_peer_to_config(self, public_key: str, psk: str, client_ip: str) -> None:
        peer_block = f"\n[Peer]\nPublicKey = {public_key}\nPresharedKey = {psk}\nAllowedIPs = {client_ip}/32\n"
        append_cmd = f"docker exec -i {self.container_name} bash -c 'echo -e \"{peer_block}\" >> {self.config_path}'"
        await self._cmd(append_cmd)

    async def _remove_peer_from_config(self, public_key: str) -> None:
        config_text = await self._cmd(
            f"docker exec -i {self.container_name} cat {self.config_path}"
        )

        blocks = config_text.split("[Peer]")
        new_blocks = [blocks[0]]

        for block in blocks[1:]:
            if public_key not in block:
                new_blocks.append(block)

        new_config_text = "[Peer]".join(new_blocks)

        write_cmd = f"docker exec -i {self.container_name} bash -c 'cat > {self.config_path}'"
        await self._cmd(write_cmd, input_data=new_config_text)

    async def _peer_in_config(self, public_key: str) -> bool:
        check_cmd = f"docker exec -i {self.container_name} grep -q {public_key} {self.config_path} && echo 'EXISTS' || echo 'MISSING'"
        status = await self._cmd(check_cmd)
        return "EXISTS" in status

    async def _kick_peer_from_kernel(self, public_key: str) -> None:
        kick_cmd = f"docker exec -i {self.container_name} awg set {self.interface_name} peer {public_key} remove"
        await self._cmd(kick_cmd)

    # ── CRUD ──────────────────────────────────────────────────────────

    async def add_client(
        self,
        inbound: Inbound,
        subscription: Subscription,
        client_uuid: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        priv_key_cmd = f"docker exec -i {self.container_name} awg genkey"
        private_key = (await self._cmd(priv_key_cmd)).strip()

        public_key = (await self._cmd(
            f"docker exec -i {self.container_name} awg pubkey",
            input_data=private_key,
        )).strip()

        psk = (await self._get_server_psk()).strip()
        next_ip = await self._find_next_free_ip(inbound)

        await self._add_peer_to_config(public_key, psk, next_ip)
        await self._sync_config()

        srv_params = await self._get_awg_server_params()

        return {
            "uuid": client_uuid or str(uuid.uuid4()),
            "public_key": public_key,
            "private_key": private_key,
            "psk": psk,
            "client_ip": next_ip,
            "server_params": srv_params,
        }

    async def remove_client(self, inbound: Inbound, connection: InboundConnection) -> bool:
        public_key = connection.public_key
        if not public_key:
            return False

        try:
            await self._kick_peer_from_kernel(public_key)
            await self._remove_peer_from_config(public_key)
            await self._sync_config()
            return True
        except Exception as e:
            logger.error("Не удалось удалить AWG-клиента: {}", e)
            return False

    async def update_client(
        self,
        inbound: Inbound,
        connection: InboundConnection,
        new_total_gb: int | None = None,
        new_expiry_date: datetime | None = None,
    ) -> bool:
        return True

    async def enable_client(self, inbound: Inbound, connection: InboundConnection) -> bool:
        public_key = connection.public_key
        psk = connection.psk
        client_ip = connection.client_ip

        if not public_key or not psk or not client_ip:
            logger.error("Отсутствуют ключи или IP в AWG-соединении клиента")
            return False

        try:
            if not await self._peer_in_config(public_key):
                await self._add_peer_to_config(public_key, psk, client_ip)
            await self._sync_config()
            return True
        except Exception as e:
            logger.error("Не удалось включить AWG-клиента: {}", e)
            return False

    async def disable_client(self, inbound: Inbound, connection: InboundConnection) -> bool:
        public_key = connection.public_key
        if not public_key:
            return False

        try:
            await self._kick_peer_from_kernel(public_key)
            await self._remove_peer_from_config(public_key)
            await self._sync_config()
            return True
        except Exception as e:
            logger.error("Не удалось отключить AWG-клиента: {}", e)
            return False

    async def reset_client_traffic(
        self, inbound: Inbound, connection: InboundConnection
    ) -> bool:
        return True

    async def get_client_traffic(
        self, inbound: Inbound, connection: InboundConnection
    ) -> dict[str, Any] | None:
        return None

    # ── Config generation ─────────────────────────────────────────────

    @staticmethod
    def _get_subnet_address(sp: dict[str, str]) -> str:
        addr = sp.get("Address", "10.8.0.1/24")
        if "/" in addr:
            ip_str = addr.split("/")[0]
            parts = ip_str.split(".")
            parts[-1] = "0"
            return ".".join(parts)
        return "10.8.0.0"

    @staticmethod
    def _encode_vpn_uri(server_config: dict) -> str:
        """Encode server config as vpn:// URI matching original AmneziaVPN format.

        Pipeline: JSON → zlib compress (Qt qCompress format: 4-byte BE size + deflate)
                  → Base64URL (no padding) → prepend "vpn://"
        """
        json_bytes = json.dumps(server_config, ensure_ascii=False).encode("utf-8")
        compressed = zlib.compress(json_bytes, 8)
        qt_compressed = struct.pack(">I", len(json_bytes)) + compressed
        b64 = base64.urlsafe_b64encode(qt_compressed).rstrip(b"=").decode("utf-8")
        return f"vpn://{b64}"

    def _generate_qr_code(self, config_str: str) -> str:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(config_str)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode("utf-8")

    async def get_client_config(
        self, inbound: Inbound, connection: InboundConnection, prefer_json: bool = True
    ) -> dict[str, Any]:
        awg = self.server.awg_service
        if not awg:
            return {"config_type": "empty", "config_data": None}

        private_key = connection.private_key.strip() if connection.private_key else ""
        public_key = connection.public_key.strip() if connection.public_key else ""
        client_ip = connection.client_ip.strip() if connection.client_ip else ""
        psk = connection.psk.strip() if connection.psk else ""

        host = self.server.ip_address
        port = str(awg.port)
        server_pub_key = awg.server_public_key
        if not server_pub_key:
            server_pub_key = (await self._get_server_public_key()).strip()

        obfuscation = awg.obfuscation or {}
        obfs_keys = ["Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"]
        i_keys = ["I1", "I2", "I3", "I4", "I5"]
        i_params = self._get_i_params()

        config_lines = [
            "[Interface]",
            f"Address = {client_ip}/32",
            "DNS = 1.1.1.1, 8.8.8.8",
            f"PrivateKey = {private_key}",
        ]
        for key in obfs_keys:
            val = obfuscation.get(key)
            if val:
                config_lines.append(f"{key} = {val}")
        if _is_v3_obfuscation(obfuscation):
            # Формат 3.1: ключ шифрования заголовков общий для клиента и сервера,
            # тайминги берём из obfuscation сервера (fallback — дефолты 3.1).
            v3_client_params = {
                "HeaderProtectionKey": obfuscation["HeaderProtectionKey"],
                **{k: obfuscation.get(k) or v for k, v in V3_TIMING_DEFAULTS.items()},
                "RandomTrailers": obfuscation.get("RandomTrailers") or "on",
                "DisableCookies": obfuscation.get("DisableCookies") or "on",
            }
        else:
            v3_client_params = {}
        for key, val in v3_client_params.items():
            if val:
                config_lines.append(f"{key} = {val}")
        for key in i_keys:
            val = i_params.get(key, "")
            if val:
                config_lines.append(f"{key} = {val}")

        config_lines.extend([
            "",
            "[Peer]",
            f"PublicKey = {server_pub_key}",
            f"PresharedKey = {psk}",
            "AllowedIPs = 0.0.0.0/0, ::/0",
            f"Endpoint = {host}:{port}",
            "PersistentKeepalive = 25",
        ])
        config_str = "\n".join(config_lines)

        # Порт здесь обязан быть числом: клиент читает его как int и молча
        # теряет строковое значение, оставляя подключение без endpoint'а.
        amnezia_json = {
            "client_priv_key": private_key,
            "client_pub_key": public_key,
            "clientId": public_key,
            "client_ip": client_ip,
            "psk_key": psk,
            "server_pub_key": server_pub_key,
            "hostName": host,
            "port": int(awg.port),
            "mtu": "1280",
            "persistent_keep_alive": "25",
            "allowed_ips": ["0.0.0.0/0", "::/0"],
            "config": config_str,
        }
        for key in obfs_keys:
            amnezia_json[key] = obfuscation.get(key, "")
        for key, val in v3_client_params.items():
            if val:
                amnezia_json[key] = val
        # Пустые I-параметры доезжают до движка как `i2=` без значения и ломают
        # разбор конфига, поэтому передаём только заполненные.
        for key in i_keys:
            if val := i_params.get(key, ""):
                amnezia_json[key] = val

        subnet = f"{awg.subnet_ip}/{awg.subnet_cidr}" if awg.subnet_ip else "10.8.0.0/24"
        is_v3 = bool(v3_client_params)
        awg_settings = {
            "port": port,
            "transport_proto": "udp",
            "subnet_address": subnet,
            "mtu": "1280",
            "protocol_version": "3.1" if is_v3 else "2",
            "last_config": json.dumps(amnezia_json, ensure_ascii=False),
        }
        if is_v3:
            # Официальный экспорт 3.1 дублирует параметры обфускации на уровень
            # контейнера, а не только в last_config. Пустые I2-I5 не пишем —
            # клиент может скормить их движку как пустые значения.
            for key in (*obfs_keys, *v3_client_params, *i_keys):
                val = v3_client_params.get(key) or obfuscation.get(key) or i_params.get(key)
                if val:
                    awg_settings[key] = val
        awg_container = {
            "container": "amnezia-awg2",
            "awg": awg_settings,
        }
        server_config = {
            "hostName": host,
            "description": self.server.name or "VPN Server",
            **({"format_version": 1} if is_v3 else {}),
            "dns1": "1.1.1.1",
            "dns2": "8.8.8.8",
            "defaultContainer": "amnezia-awg2",
            "containers": [awg_container],
        }

        vpn_uri = self._encode_vpn_uri(server_config)

        return {
            "config_type": "file",
            "config_data": config_str,
            "filename": f"AWG_{connection.subscription.name}.conf",
            "vpn_uri": vpn_uri,
            "amnezia_json": amnezia_json,
        }

    async def close(self) -> None:
        pass
