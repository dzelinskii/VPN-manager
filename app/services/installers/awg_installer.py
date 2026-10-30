"""AmneziaWG installer following original AmneziaVPN logic (AWG 3.1 format).

Reference: amnezia-client/client/server_scripts/awg/

Flow:
1. prepare_host() — Docker, utils, UFW (via BaseInstaller)
2. generate_obfuscation_params() — 3.1 defaults: Jc random 4-6, Jmin=10, Jmax=50,
   S1-S4=12 (HeaderProtectionKey требует S1-S4 >= 12), H1-H4=1..4,
   RandomTrailers=on, DisableCookies=on
3. install(port, subnet_cidr, obfuscation_params) — full installation
4. Saves config to /opt/vpnbot/awg/ on the server

Container name: vpnbot-awg
Config dir on server: /opt/vpnbot/awg/

Клиентские конфиги для таких серверов требуют приложение AmneziaVPN >= 5.0.1.5.
"""

import contextlib
import random
import re
from typing import Any

from loguru import logger

from app.services.installers.base import BASE_DIR, AlreadyInstalledError, BaseInstaller

AWG_SERVICE_DIR = f"{BASE_DIR}/awg"
AWG_CONTAINER_NAME = "awg"
AWG_INTERFACE = "awg0"
# Контейнеры AmneziaWG официального приложения (client/core/utils/containers/
# containerUtils.cpp): amnezia-awg2 — актуальный (AWG 3.x, тулзы awg),
# amnezia-awg — legacy (тулзы wg, конфиг wg0.conf).
OFFICIAL_AWG_CONTAINERS = ("amnezia-awg2", "amnezia-awg")
LEGACY_OFFICIAL_CONTAINER = "amnezia-awg"
LEGACY_CONFIG_PATH = "/opt/amnezia/awg/wg0.conf"
AWG_CONFIG_PATH = "/opt/amnezia/awg/awg0.conf"

AWG_SUBNET_DEFAULT = "10.8.0.0"
AWG_SUBNET_CIDR_DEFAULT = 24
AWG_SUBNET_IP_DEFAULT = "10.8.0.1"


def parse_awg_conf(config_text: str) -> dict:
    """Разобрать [Interface]-секцию awg0.conf/wg0.conf (свой или app-контейнера).

    Возвращает port, subnet_ip, subnet_cidr, obfuscation (включая
    HeaderProtectionKey и закомментированные I1-I5), server_private_key и
    protocol_version ("3.1" | "2" | "unknown") — эвристика как в
    awgProtocolConfig.cpp:20-65 официального клиента.
    """
    port_match = re.search(r"ListenPort\s*=\s*(\d+)", config_text)
    addr_match = re.search(r"Address\s*=\s*([\d.]+)/(\d+)", config_text)
    pk_match = re.search(r"PrivateKey\s*=\s*(\S+)", config_text)

    obfuscation: dict[str, str] = {}
    for key in _OBF_KEYS:
        m = re.search(rf"{key}\s*=\s*(.+)", config_text)
        if m:
            obfuscation[key] = m.group(1).strip()
    for key in _I_KEYS:
        m = re.search(rf"{key}\s*=\s*(.*)", config_text)
        obfuscation[key] = m.group(1).strip() if m else ""
    m = re.search(r"HeaderProtectionKey\s*=\s*(\S+)", config_text)
    if m:
        obfuscation["HeaderProtectionKey"] = m.group(1).strip()

    if obfuscation.get("HeaderProtectionKey"):
        protocol_version = "3.1"
    elif obfuscation.get("S3") or obfuscation.get("S4"):
        protocol_version = "2"
    else:
        protocol_version = "unknown"

    return {
        "port": int(port_match.group(1)) if port_match else None,
        "subnet_ip": addr_match.group(1) if addr_match else None,
        "subnet_cidr": int(addr_match.group(2)) if addr_match else None,
        "obfuscation": obfuscation,
        "server_private_key": pk_match.group(1).strip() if pk_match else "",
        "protocol_version": protocol_version,
    }


I1_DEFAULT = "<r 2><b 0x858000010001000000000669636c6f756403636f6d0000010001c00c000100010000105a00044d583737>"

# Тайминги 3.1: официальный клиент AmneziaVPN пишет их и в серверный, и в
# клиентский конфиг (диапазоны — клиент рандомизирует при каждом запуске).
V3_TIMING_DEFAULTS = {
    "RekeyAfterTime": "100-120",
    "RekeyTimeout": "3-7",
    "RejectAfterTime": "150-180",
    "KeepaliveTimeout": "5-15",
    "MaxHandshakeAttempts": "15-20",
}

# Читаемые из awg0.conf параметры обфускации (I1-I5 в конфиге закомментированы —
# так их пишет и официальный клиент, парсим обе формы).
_OBF_KEYS = (
    "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4",
    "RandomTrailers", "DisableCookies", *V3_TIMING_DEFAULTS,
)
_I_KEYS = ("I1", "I2", "I3", "I4", "I5")


def generate_obfuscation_params() -> dict[str, str]:
    """Generate AWG 3.1 obfuscation parameters matching original AmneziaVPN.

    S1-S4: fixed 12 (HeaderProtectionKey требует S1-S4 >= 12).
    H1-H4: фиксированные 1..4 — случайные диапазоны в 3.x больше не нужны.
    RandomTrailers/DisableCookies: новые тумблеры 3.1, всегда on.
    Тайминги Rekey*/Reject*/Keepalive*: общие для сервера и клиента.
    I1: DNS A response for icloud.com (default anti-DPI junk).
    I2-I5: empty (reserved for future use).
    HeaderProtectionKey генерируется отдельно на сервере (awg genkey)
    в _generate_keys_and_config и добавляется в obfuscation при установке.
    """
    return {
        "Jc": str(random.randint(4, 6)),
        "Jmin": "10",
        "Jmax": "50",
        "S1": "12",
        "S2": "12",
        "S3": "12",
        "S4": "12",
        "H1": "1",
        "H2": "2",
        "H3": "3",
        "H4": "4",
        "RandomTrailers": "on",
        "DisableCookies": "on",
        **V3_TIMING_DEFAULTS,
        "I1": I1_DEFAULT,
        "I2": "",
        "I3": "",
        "I4": "",
        "I5": "",
    }


def build_awg0_conf(
    private_key: str,
    subnet_ip: str,
    subnet_cidr: int,
    port: int,
    header_protection_key: str,
    obfuscation: dict[str, str],
) -> str:
    """Собрать серверный awg0.conf.

    Порядок строк повторяет конфиг официального приложения AmneziaVPN 3.1:
    Jc..H4, HeaderProtectionKey, тайминги, RandomTrailers/DisableCookies,
    закомментированные I1-I5.
    """
    obf_keys = ["Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"]
    v3_keys = ["RandomTrailers", "DisableCookies"]
    i_keys = ["I1", "I2", "I3", "I4", "I5"]

    obf_lines = "\n".join(f"{k} = {obfuscation[k]}" for k in obf_keys if obfuscation.get(k))
    timing_lines = "\n".join(f"{k} = {obfuscation[k]}" for k in V3_TIMING_DEFAULTS if obfuscation.get(k))
    v3_lines = "\n".join(f"{k} = {obfuscation[k]}" for k in v3_keys if obfuscation.get(k))
    i_lines = "\n".join(f"# {k} = {obfuscation.get(k, '')}" for k in i_keys)

    return (
        f"[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = {subnet_ip}/{subnet_cidr}\n"
        f"ListenPort = {port}\n"
        f"{obf_lines}\n"
        f"HeaderProtectionKey = {header_protection_key}\n"
        f"{timing_lines}\n"
        f"{v3_lines}\n"
        f"{i_lines}\n"
    )


class AWGInstaller(BaseInstaller):
    """Installer for AmneziaWG service."""

    SERVICE_NAME = AWG_CONTAINER_NAME

    async def _bot_managed_awg_names(self) -> set[str]:
        """Имена AWG-контейнеров, установленных ботом (по метке vpnbot.service=awg).

        Бот ставит метку в docker-compose; у контейнеров официального приложения
        её нет. Имя контейнера признаком быть не может: бот может поставить
        контейнер с именем amnezia-awg2, чтобы приложение его подобрало.
        """
        try:
            result = await self._cmd(
                "docker ps -a --filter label=vpnbot.service=awg --format '{{.Names}}'"
            )
        except Exception as e:
            import asyncssh

            if isinstance(e, (asyncssh.Error, OSError)) or "SSH" in str(e):
                raise
            return set()
        return {n.strip() for n in result.strip().splitlines() if n.strip()}

    async def list_awg_containers(self) -> list[dict[str, Any]]:
        """Найти AWG-контейнеры на сервере: свои (по метке) и приложения.

        Возвращает [{"name": ..., "external": bool}] — порядок приоритета:
        свой контейнер первым, затем amnezia-awg2 (актуальный), amnezia-awg (legacy).
        """
        try:
            result = await self._cmd("docker ps -a --format '{{.Names}}'")
        except Exception as e:
            import asyncssh

            if isinstance(e, (asyncssh.Error, OSError)) or "SSH" in str(e):
                raise
            return []
        names = {line.strip() for line in result.strip().splitlines() if line.strip()}
        managed = await self._bot_managed_awg_names()

        found: list[dict[str, Any]] = []
        for name in (f"vpnbot-{AWG_CONTAINER_NAME}", *OFFICIAL_AWG_CONTAINERS):
            if name in names:
                found.append({"name": name, "external": name not in managed})
        return found

    async def check_already_installed(self) -> bool:
        """AWG считается установленным, если есть любой AWG-контейнер."""
        return bool(await self.list_awg_containers())

    async def discover_existing(self, container_name: str = "vpnbot-awg") -> dict:
        """Прочитать конфигурацию существующего AWG-контейнера через docker exec.

        Работает и с контейнером приложения (у него нет volume — конфиг живёт
        в ФС контейнера), и со своим vpnbot-awg.

        Returns dict with: port, subnet_ip, subnet_cidr, obfuscation,
        server_public_key, server_private_key, container_name, is_external,
        protocol_version.
        """
        legacy = container_name == LEGACY_OFFICIAL_CONTAINER
        config_path = LEGACY_CONFIG_PATH if legacy else AWG_CONFIG_PATH
        tool = "wg" if legacy else "awg"

        async with self.ssh:
            def _dex(cmd: str, input_data: str | None = None) -> Any:
                return self._cmd(
                    f"docker exec -i {container_name} {cmd}", input_data=input_data
                )

            config = await _dex(f"cat {config_path}")
            parsed = parse_awg_conf(config)

            public_key = ""
            with contextlib.suppress(Exception):
                public_key = (await _dex(
                    "cat /opt/amnezia/awg/wireguard_server_public_key.key"
                )).strip()
            if not public_key and parsed["server_private_key"]:
                # Файла pubkey нет (или пуст) — выводим из приватного ключа.
                with contextlib.suppress(Exception):
                    public_key = (await _dex(
                        f"sh -c '{tool} pubkey'",
                        input_data=parsed["server_private_key"] + "\n",
                    )).strip()

            # Внешний = без метки бота: бот может ставить контейнер и с именем
            # amnezia-awg2 (чтобы приложение его подобрало), имя не маркер.
            managed = await self._bot_managed_awg_names()

        return {
            "port": parsed["port"] or 51820,
            "subnet_ip": parsed["subnet_ip"] or "10.8.0.1",
            "subnet_cidr": parsed["subnet_cidr"] or 24,
            "obfuscation": parsed["obfuscation"],
            "server_public_key": public_key,
            "server_private_key": parsed["server_private_key"],
            "container_name": container_name,
            "is_external": container_name not in managed,
            "protocol_version": parsed["protocol_version"],
        }

    async def install(
        self,
        port: int,
        subnet_ip: str = AWG_SUBNET_IP_DEFAULT,
        subnet_cidr: int = AWG_SUBNET_CIDR_DEFAULT,
        obfuscation: dict[str, str] | None = None,
        force: bool = False,
        container_name: str = f"vpnbot-{AWG_CONTAINER_NAME}",
    ) -> dict:
        """Install AmneziaWG on the server.

        Args:
            port: UDP port for AWG to listen on.
            subnet_ip: Server IP within the VPN subnet (e.g. '10.8.0.1').
            subnet_cidr: Subnet CIDR mask (e.g. 24).
            obfuscation: AWG obfuscation params. Auto-generated if None.
            force: Remove existing container before installing.
            container_name: Имя docker-контейнера. По умолчанию vpnbot-awg;
                можно задать amnezia-awg2 — тогда официальный клиент Amnezia
                подхватит контейнер при добавлении сервера (он ищет контейнеры
                по этому имени, installController.cpp).

        Returns:
            Dict with installation details (port, subnet, obfuscation params).

        Raises:
            AlreadyInstalledError: If service already installed and force=False.
            RuntimeError: If port is occupied.
        """
        service_dir = f"{AWG_SERVICE_DIR}"
        dirs_to_clean: list[str] = []
        ports_to_clean: list[tuple[int, str]] = []

        try:
            async with self.ssh:
                logger.info(
                    "Установка AWG на {}:{}, subnet={}/{}, container={}",
                    self.ssh.host, port, subnet_ip, subnet_cidr, container_name,
                )

                await self._progress(1, 11, "Подготовка сервера (Docker, утилиты)...")
                await self.prepare_host()

                if await self.check_already_installed():
                    existing = await self.list_awg_containers()
                    existing_names = ", ".join(c["name"] for c in existing)
                    if force:
                        # Удаляем только «свой» контейнер; внешние (приложения)
                        # не трогаем — их переустановка стирает чужих пиров.
                        logger.warning(
                            "Принудительная переустановка: удаляю {} на {}",
                            container_name, self.ssh.host,
                        )
                        await self._cmd(
                            f"docker rm -f {container_name} 2>/dev/null || true"
                        )
                        await self._cmd("sleep 2")
                    else:
                        raise AlreadyInstalledError(
                            f"AmneziaWG уже установлен на {self.ssh.host} "
                            f"(контейнер(ы): {existing_names}). "
                            "Для переустановки нажмите кнопку ниже."
                        )

                if not await self.check_port_free(port):
                    raise RuntimeError(f"Port {port}/udp is occupied on {self.ssh.host}")

                if obfuscation is None:
                    obfuscation = generate_obfuscation_params()

                dirs_to_clean = [service_dir]
                ports_to_clean = [(port, "udp")]

                await self._progress(2, 11, "Открытие портов в файрволе...")
                await self._open_firewall_port(port)

                await self._progress(3, 11, "Создание директорий...")
                await self._create_service_dir(service_dir)

                await self._progress(4, 11, "Запись Dockerfile...")
                await self._write_dockerfile(service_dir)

                await self._progress(5, 11, "Запись стартового скрипта...")
                await self._write_start_script(service_dir, subnet_ip, subnet_cidr)

                await self._progress(6, 11, "Запись docker-compose.yml...")
                await self._write_compose_file(service_dir, port, container_name)

                await self._progress(7, 11, "Сборка Docker-образа (может занять 1-2 мин)...")
                await self._build_image()

                await self._progress(8, 11, "Запуск контейнера...")
                await self._start_container(port)

                await self._progress(9, 11, "Генерация ключей и конфигурации...")
                keys = await self._generate_keys_and_config(
                    port, subnet_ip, subnet_cidr, obfuscation, container_name
                )
                obfuscation = {
                    **obfuscation,
                    "HeaderProtectionKey": keys["header_protection_key"],
                }

                await self._progress(10, 11, "Перезапуск с конфигурацией...")
                await self._restart_container(container_name)

                await self._progress(11, 11, "Установка завершена")

                logger.info("AWG установлен на {}:{} (контейнер {})", self.ssh.host, port, container_name)

                return {
                    "container_name": container_name,
                    "port": port,
                    "subnet_ip": subnet_ip,
                    "subnet_cidr": subnet_cidr,
                    "obfuscation": obfuscation,
                    "service_dir": service_dir,
                    "server_private_key": keys["server_private_key"],
                    "server_public_key": keys["server_public_key"],
                    "header_protection_key": keys["header_protection_key"],
                }
        except Exception:
            logger.exception("AWG installation failed, running cleanup")
            await self.cleanup(
                dirs=dirs_to_clean,
                ports=ports_to_clean,
            )
            raise

    async def _open_firewall_port(self, port: int) -> None:
        await self.port_manager.open_port(port, "udp")

    async def _create_service_dir(self, service_dir: str) -> None:
        await self.ensure_dirs(service_dir)

    async def _write_dockerfile(self, service_dir: str) -> None:
        dockerfile = (
            "FROM amneziavpn/amneziawg-go:latest\n"
            "LABEL maintainer=\"vpnbot\"\n"
            "RUN apk add --no-cache bash curl dumb-init\n"
            "RUN apk --update upgrade --no-cache\n"
            "RUN mkdir -p /opt/amnezia\n"
            "COPY start.sh /opt/amnezia/start.sh\n"
            "RUN chmod a+x /opt/amnezia/start.sh\n"
        )
        await self._write_file(f"{service_dir}/Dockerfile", dockerfile)

    async def _write_start_script(
        self, service_dir: str, subnet_ip: str, subnet_cidr: int
    ) -> None:
        script = f"""#!/bin/bash
echo "Container startup"
awg-quick down /opt/amnezia/awg/awg0.conf 2>/dev/null || true

if [ -f /opt/amnezia/awg/awg0.conf ]; then awg-quick up /opt/amnezia/awg/awg0.conf; fi

iptables -A INPUT -i awg0 -j ACCEPT
iptables -A FORWARD -i awg0 -j ACCEPT
iptables -A OUTPUT -o awg0 -j ACCEPT

iptables -A FORWARD -i awg0 -o eth0 -s {subnet_ip}/{subnet_cidr} -j ACCEPT
iptables -A FORWARD -i awg0 -o eth1 -s {subnet_ip}/{subnet_cidr} -j ACCEPT
iptables -A FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT

iptables -t nat -A POSTROUTING -s {subnet_ip}/{subnet_cidr} -o eth0 -j MASQUERADE
iptables -t nat -A POSTROUTING -s {subnet_ip}/{subnet_cidr} -o eth1 -j MASQUERADE

tail -f /dev/null
"""
        await self._write_file(f"{service_dir}/start.sh", script)

    async def _write_compose_file(
        self, service_dir: str, port: int, container_name: str
    ) -> None:
        compose = f"""services:
  awg:
    build: {service_dir}
    container_name: {container_name}
    restart: unless-stopped
    entrypoint: ["/opt/amnezia/start.sh"]
    privileged: true
    cap_add:
      - NET_ADMIN
      - SYS_MODULE
    ports:
      - "{port}:{port}/udp"
    volumes:
      - {service_dir}/data:/opt/amnezia/awg
      - /lib/modules:/lib/modules:ro
    sysctls:
      - net.ipv4.conf.all.src_valid_mark=1
    labels:
      - "vpnbot.service=awg"
"""
        await self._write_file(f"{service_dir}/docker-compose.yml", compose)

    async def _build_image(self) -> None:
        await self._cmd(f"cd {AWG_SERVICE_DIR} && docker compose build --no-cache")

    async def _start_container(self, port: int) -> None:
        await self._cmd(f"cd {AWG_SERVICE_DIR} && docker compose up -d")
        await self._cmd("sleep 5")

    async def _generate_keys_and_config(
        self,
        port: int,
        subnet_ip: str,
        subnet_cidr: int,
        obfuscation: dict[str, str],
        container_name: str = f"vpnbot-{AWG_CONTAINER_NAME}",
    ) -> dict[str, str]:
        name = container_name

        private_key = await self._cmd(
            f"docker exec -i {name} awg genkey"
        )
        public_key = await self._cmd(
            f"docker exec -i {name} awg pubkey",
            input_data=private_key.strip(),
        )
        psk = await self._cmd(
            f"docker exec -i {name} awg genpsk"
        )
        header_protection_key = await self._cmd(
            f"docker exec -i {name} awg genkey"
        )

        key_files = {
            "wireguard_server_private_key.key": private_key,
            "wireguard_server_public_key.key": public_key,
            "wireguard_psk.key": psk,
            "header_protection_key.key": header_protection_key,
        }
        for filename, content in key_files.items():
            await self._cmd(
                f"docker exec -i {name} bash -c 'cat > /opt/amnezia/awg/{filename}'",
                input_data=content,
            )

        config = build_awg0_conf(
            private_key.strip(),
            subnet_ip,
            subnet_cidr,
            port,
            header_protection_key.strip(),
            obfuscation,
        )

        await self._cmd(
            f"docker exec -i {name} bash -c 'cat > /opt/amnezia/awg/awg0.conf'",
            input_data=config,
        )

        return {
            "server_private_key": private_key,
            "server_public_key": public_key,
            "header_protection_key": header_protection_key,
        }

    async def _restart_container(
        self, container_name: str = f"vpnbot-{AWG_CONTAINER_NAME}"
    ) -> None:
        await self._cmd(f"docker restart {container_name}")
        await self._cmd("sleep 3")
