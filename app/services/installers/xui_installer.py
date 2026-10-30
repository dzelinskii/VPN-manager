"""3x-ui panel installer with Caddy reverse-proxy.

Architecture:
- 3x-ui container (network_mode: host) — panel on :2053, subscription on :2096
- Caddy container (network_mode: host) — reverse-proxy with auto-SSL on :{caddy_port}
- Caddy proxies /{web_path}/* -> :2053, /{sub_path}/* and /{json_path}/* -> :2096
- UFW blocks direct access to 2053/2096, only Caddy port is open

After container start, 3x-ui SQLite DB is modified directly to set:
custom paths, credentials, ports. Then container is restarted.

Container names: vpnbot-xui (3x-ui), vpnbot-caddy (Caddy)
Config dir on server: /opt/vpnbot/xui/
"""

import bcrypt
from loguru import logger

from app.services.installers.base import BASE_DIR, AlreadyInstalledError, BaseInstaller

XUI_SERVICE_DIR = f"{BASE_DIR}/xui"
XUI_CONTAINER = "xui"
CADDY_CONTAINER = "caddy"

XUI_INTERNAL_PORT = 2053
XUI_SUB_PORT = 2096


def _sql_str(value: str) -> str:
    """Escape a string for safe inlining into a single-quoted SQLite literal."""
    return value.replace("'", "''")


def _parse_port_ranges(text: str) -> list[tuple[int, int]]:
    """Parse port input like '443, 10000-10100, 666' into list of (start, end) tuples.

    Raises ValueError on invalid input.
    """
    ranges = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            start = int(bounds[0].strip())
            end = int(bounds[1].strip())
            if start < 1 or end > 65535 or start > end:
                raise ValueError(f"Invalid range: {part}")
            ranges.append((start, end))
        else:
            port = int(part)
            if port < 1 or port > 65535:
                raise ValueError(f"Invalid port: {port}")
            ranges.append((port, port))
    if not ranges:
        raise ValueError("No valid ports provided")
    return ranges


def _flatten_ranges(ranges: list[tuple[int, int]]) -> list[int]:
    """Flatten port ranges into list of individual ports."""
    ports = []
    for start, end in ranges:
        ports.extend(range(start, end + 1))
    return ports


class XUIInstaller(BaseInstaller):
    """Installer for 3x-ui panel with Caddy reverse-proxy."""

    SERVICE_NAME = XUI_CONTAINER

    async def check_already_installed(self) -> bool:
        """Check if vpnbot-xui OR vpnbot-caddy container exists."""
        for name in ("vpnbot-xui", "vpnbot-caddy"):
            try:
                result = await self._cmd(
                    f"docker ps -a --filter name=^{name}$ --format '{{{{.Names}}}}'"
                )
                if result.strip():
                    return True
            except Exception as e:
                import asyncssh
                if isinstance(e, (asyncssh.Error, OSError)) or "SSH" in str(e):
                    raise
                return False
        return False

    async def discover_existing(self) -> dict:
        """Read configs from an existing installation and return params.

        Reads domain/port from Caddyfile, paths from 3x-ui SQLite DB.
        Password is bcrypt-hashed and cannot be recovered.
        """
        import re

        async with self.ssh:
            caddyfile = await self._cmd(f"cat {XUI_SERVICE_DIR}/caddy/Caddyfile")

            port_match = re.search(r"^([\w.\-]+):(\d+)\s*\{", caddyfile, re.MULTILINE)
            domain = port_match.group(1) if port_match else None
            caddy_port = int(port_match.group(2)) if port_match else 8443

            web_path = "/"
            sub_path = "/sub/"
            sub_json_path = "/json/"
            username = "admin"

            try:
                db = "docker exec -i vpnbot-xui sqlite3 /etc/x-ui/x-ui.db"

                row = await self._cmd(f'{db} "SELECT username FROM users LIMIT 1"')
                if row.strip():
                    username = row.strip()

                row = await self._cmd(f"{db} \"SELECT value FROM settings WHERE key='webBasePath'\"")
                if row.strip():
                    web_path = row.strip()
                    if not web_path.endswith("/"):
                        web_path += "/"

                row = await self._cmd(f"{db} \"SELECT value FROM settings WHERE key='subPath'\"")
                if row.strip():
                    sub_path = row.strip()
                    if not sub_path.endswith("/"):
                        sub_path += "/"

                row = await self._cmd(f"{db} \"SELECT value FROM settings WHERE key='subJsonPath'\"")
                if row.strip():
                    sub_json_path = row.strip()
                    if not sub_json_path.endswith("/"):
                        sub_json_path += "/"
            except Exception:
                pass

        return {
            "domain": domain or self.ssh.host,
            "caddy_port": caddy_port,
            "web_path": web_path,
            "sub_path": sub_path,
            "sub_json_path": sub_json_path,
            "username": username,
        }

    async def install(
        self,
        domain: str,
        caddy_port: int = 8443,
        web_path: str = "/",
        sub_path: str = "/sub/",
        sub_json_path: str = "/json/",
        username: str = "admin",
        password: str = "admin",
        inbound_ranges: list[tuple[int, int]] | None = None,
        force: bool = False,
    ) -> dict:
        """Install 3x-ui with Caddy reverse-proxy.

        Args:
            domain: Domain name or IP for Caddy SSL.
            caddy_port: HTTPS port for Caddy to listen on.
            web_path: webBasePath for 3x-ui panel.
            sub_path: Subscription path.
            sub_json_path: Subscription JSON path.
            username: 3x-ui admin username.
            password: 3x-ui admin password.
            inbound_ranges: Port ranges for VPN inbounds (opened in UFW).

        Returns:
            Dict with installation details.

        Raises:
            RuntimeError: If service already installed or port occupied.
        """
        if inbound_ranges is None:
            inbound_ranges = [(10000, 10100)]

        service_dir = XUI_SERVICE_DIR
        dirs_to_clean = [service_dir]
        ports_to_clean: list[tuple[int, str]] = [
            (caddy_port, "tcp"),
            (caddy_port, "udp"),
            (80, "tcp"),
        ]
        for start, end in inbound_ranges:
            for port in range(start, end + 1):
                ports_to_clean.append((port, "tcp"))
                ports_to_clean.append((port, "udp"))

        try:
            async with self.ssh:
                logger.info(
                    "Установка 3x-ui на {}, domain={}, caddy_port={}",
                    self.ssh.host, domain, caddy_port,
                )

                await self._progress(1, 9, "Подготовка сервера (Docker, утилиты)...")
                await self.prepare_host()

                if await self.check_already_installed():
                    if force:
                        logger.warning("Принудительная переустановка: удаляю vpnbot-xui/caddy на {}", self.ssh.host)
                        await self._cmd("docker rm -f vpnbot-xui vpnbot-caddy 2>/dev/null || true")
                        await self._cmd("sleep 2")
                    else:
                        raise AlreadyInstalledError(
                            f"3x-ui уже установлен на {self.ssh.host}. "
                            "Для переустановки нажмите кнопку ниже."
                        )

                if not await self.check_port_free(caddy_port):
                    raise RuntimeError(f"Port {caddy_port} is occupied on {self.ssh.host}")

                await self._progress(2, 9, "Открытие портов в файрволе...")
                await self._open_firewall_ports(caddy_port, inbound_ranges)
                await self._block_internal_ports()

                await self._progress(3, 9, "Создание директорий...")
                await self._create_dirs(service_dir)

                await self._progress(4, 9, "Запись docker-compose.yml...")
                await self._write_compose_file(service_dir, domain, caddy_port)

                await self._progress(5, 9, "Запись Caddyfile...")
                await self._write_caddyfile(service_dir, domain, caddy_port, sub_path, sub_json_path, web_path)

                await self._progress(6, 9, "Запуск контейнеров (может занять 1-2 мин)...")
                await self._start_containers(service_dir)

                await self._progress(7, 9, "Настройка 3x-ui (credentials, пути, порты)...")
                await self._configure_xui(username, password, web_path, sub_path, sub_json_path, domain, caddy_port)

                await self._progress(8, 9, "Проверка доступности панели...")
                await self._verify_panel(caddy_port, domain, web_path)

                await self._progress(9, 9, "Установка завершена")

                logger.info("3x-ui установлен на {}", self.ssh.host)

                return {
                    "containers": ["vpnbot-xui", "vpnbot-caddy"],
                    "domain": domain,
                    "caddy_port": caddy_port,
                    "web_path": web_path,
                    "sub_path": sub_path,
                    "sub_json_path": sub_json_path,
                    "username": username,
                    "inbound_ranges": inbound_ranges,
                    "service_dir": service_dir,
                }
        except Exception:
            logger.exception("3x-ui installation failed, running cleanup")
            await self.cleanup(
                dirs=dirs_to_clean if not force else [],
                ports=ports_to_clean,
            )
            raise

    async def _open_firewall_ports(
        self, caddy_port: int, inbound_ranges: list[tuple[int, int]]
    ) -> None:
        await self.port_manager.open_port(80, "tcp")
        await self.port_manager.open_port(caddy_port, "tcp")
        await self.port_manager.open_port(caddy_port, "udp")
        for start, end in inbound_ranges:
            if start == end:
                await self._cmd(f"ufw allow {start}/tcp")
                await self._cmd(f"ufw allow {start}/udp")
            else:
                await self._cmd(f"ufw allow {start}:{end}/tcp")
                await self._cmd(f"ufw allow {start}:{end}/udp")

    async def _block_internal_ports(self) -> None:
        await self._cmd(f"ufw deny {XUI_INTERNAL_PORT}/tcp || true")
        await self._cmd(f"ufw deny {XUI_INTERNAL_PORT}/udp || true")
        await self._cmd(f"ufw deny {XUI_SUB_PORT}/tcp || true")
        await self._cmd(f"ufw deny {XUI_SUB_PORT}/udp || true")

    async def _create_dirs(self, service_dir: str) -> None:
        await self.ensure_dirs(f"{service_dir}/caddy/data", f"{service_dir}/caddy/config", f"{service_dir}/db")

    async def _write_compose_file(
        self, service_dir: str, domain: str, caddy_port: int
    ) -> None:
        compose = f"""services:
  xui:
    image: ghcr.io/mhsanaei/3x-ui:latest
    container_name: vpnbot-xui
    hostname: {domain}
    network_mode: host
    restart: unless-stopped
    volumes:
      - ./db:/etc/x-ui
    environment:
      - XRAY_VMESS_AEAD_FORCED=false
    cap_add:
      - NET_ADMIN
    labels:
      - "vpnbot.service=xui"

  caddy:
    image: caddy:2-alpine
    container_name: vpnbot-caddy
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./caddy/Caddyfile:/etc/caddy/Caddyfile
      - ./caddy/data:/data
      - ./caddy/config:/config
    labels:
      - "vpnbot.service=caddy"
"""
        await self._write_file(f"{service_dir}/docker-compose.yml", compose)

    async def _write_caddyfile(
        self,
        service_dir: str,
        domain: str,
        caddy_port: int,
        sub_path: str,
        sub_json_path: str,
        web_path: str,
    ) -> None:
        sub_path_stripped = sub_path.strip("/")
        json_path_stripped = sub_json_path.strip("/")
        web_path_stripped = web_path.strip("/")

        caddyfile = f""":80 {{
  redir https://{{{{host}}}}:{caddy_port}{{{{uri}}}}
}}

{domain}:{caddy_port} {{
  encode gzip

  log {{
    output file /data/access.log
  }}

  handle /{sub_path_stripped}/* {{
    reverse_proxy 127.0.0.1:{XUI_SUB_PORT}
  }}

  handle /{json_path_stripped}/* {{
    reverse_proxy 127.0.0.1:{XUI_SUB_PORT}
  }}

  handle /{web_path_stripped}/* {{
    reverse_proxy 127.0.0.1:{XUI_INTERNAL_PORT}
  }}

  handle /* {{
    reverse_proxy 127.0.0.1:{XUI_INTERNAL_PORT}
  }}

  respond "404" 404
}}
"""
        await self._write_file(f"{service_dir}/caddy/Caddyfile", caddyfile)

    async def _start_containers(self, service_dir: str) -> None:
        await self._cmd(f"cd {service_dir} && docker compose up -d")
        await self._cmd("sleep 8")

    async def _configure_xui(
        self,
        username: str,
        password: str,
        web_path: str,
        sub_path: str,
        sub_json_path: str,
        domain: str,
        caddy_port: int,
    ) -> None:
        name = f"vpnbot-{XUI_CONTAINER}"
        db_path = "/etc/x-ui/x-ui.db"

        clean_web = "/" + web_path.strip("/") + "/" if web_path.strip("/") else "/"
        clean_sub = "/" + sub_path.strip("/") + "/"
        clean_json = "/" + sub_json_path.strip("/") + "/"
        base_url = f"https://{domain}:{caddy_port}"
        sub_uri = f"{base_url}{clean_sub}"
        sub_json_uri = f"{base_url}{clean_json}"

        await self._cmd(f"docker exec -i {name} apk add --no-cache sqlite")

        # x-ui при первом старте недолго держит SQLite-лок (миграции/инициализация):
        # без busy-timeout правки падают с "database is locked".
        sqlite_cmd = f"docker exec -i {name} sqlite3 -cmd '.timeout 15000' {db_path}"

        sql_del = (
            "DELETE FROM settings WHERE key IN "
            "('webBasePath', 'subPath', 'subJsonPath', "
            "'subEnable', 'subJsonEnable', 'subURI', 'subJsonURI', "
            "'webPort', 'subPort')"
        )
        await self._cmd(
            sqlite_cmd,
            input_data=sql_del,
        )

        sql_ins = (
            f"INSERT INTO settings (key, value) VALUES "
            f"('webBasePath', '{_sql_str(clean_web)}'), "
            f"('subPath', '{_sql_str(clean_sub)}'), "
            f"('subJsonPath', '{_sql_str(clean_json)}'), "
            f"('subEnable', 'true'), "
            f"('subJsonEnable', 'true'), "
            f"('subURI', '{_sql_str(sub_uri)}'), "
            f"('subJsonURI', '{_sql_str(sub_json_uri)}'), "
            f"('webPort', '{XUI_INTERNAL_PORT}'), "
            f"('subPort', '{XUI_SUB_PORT}')"
        )
        await self._cmd(
            sqlite_cmd,
            input_data=sql_ins,
        )

        sql_user = (
            f"UPDATE users SET username='{_sql_str(username)}', "
            f"password='{_sql_str(self._hash_password_bcrypt(password))}' "
            f"WHERE id=1"
        )
        await self._cmd(
            sqlite_cmd,
            input_data=sql_user,
        )

        await self._cmd(f"docker restart {name}")
        await self._cmd("sleep 5")

    async def _verify_panel(
        self, caddy_port: int, domain: str, web_path: str
    ) -> None:
        clean_web = web_path.strip("/")
        xui_url = f"http://127.0.0.1:{XUI_INTERNAL_PORT}/{clean_web}/" if clean_web else f"http://127.0.0.1:{XUI_INTERNAL_PORT}/"

        result = await self._cmd(
            f"curl -s -o /dev/null -w '%{{http_code}}' {xui_url}"
        )
        code = result.strip().strip("'")
        if code not in ("200", "301", "302"):
            raise RuntimeError(
                f"Panel verification failed: HTTP {code} from {xui_url}"
            )
        logger.info("Панель доступна через внутренний порт: HTTP {}", code)

        caddy_url = f"https://{domain}:{caddy_port}/{clean_web}/" if clean_web else f"https://{domain}:{caddy_port}/"
        # TLS у Caddy может ещё не подняться: сертификат ACME выдаётся асинхронно
        # (и временно упирается в rate-limit Let's Encrypt). Это не ошибка
        # установки — панель доступна, Caddy доберёт сертификат ретраями.
        caddy_result = await self._cmd(
            f"curl -sk -o /dev/null -w '%{{http_code}}' {caddy_url} 2>/dev/null || echo no-tls-yet"
        )
        caddy_code = caddy_result.strip().strip("'")
        logger.info("Caddy-прокси: {} ({})", caddy_code, caddy_url)

    @staticmethod
    def _hash_password_bcrypt(password: str) -> str:
        return bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt(rounds=10)
        ).decode("utf-8")

    async def open_inbound_ports(self, ranges: list[tuple[int, int]]) -> None:
        """Open additional port ranges for VPN inbounds (post-install)."""
        for start, end in ranges:
            if start == end:
                await self._cmd(f"ufw allow {start}/tcp")
                await self._cmd(f"ufw allow {start}/udp")
            else:
                await self._cmd(f"ufw allow {start}:{end}/tcp")
                await self._cmd(f"ufw allow {start}:{end}/udp")

    async def close_inbound_ports(self, ranges: list[tuple[int, int]]) -> None:
        """Close port ranges for VPN inbounds (post-install)."""
        for start, end in ranges:
            if start == end:
                await self._cmd(f"ufw delete allow {start}/tcp || true")
                await self._cmd(f"ufw delete allow {start}/udp || true")
            else:
                await self._cmd(f"ufw delete allow {start}:{end}/tcp || true")
                await self._cmd(f"ufw delete allow {start}:{end}/udp || true")
