"""Base installer providing shared host preparation and cleanup for all VPN service installers.

All installers (AWG, XUI, MTProxy) inherit from BaseInstaller which handles:
- Docker installation and verification
- OS detection (multi-distro: Debian, Fedora, CentOS, openSUSE, Arch)
- Essential utilities (curl, jq)
- UFW firewall setup
- Container naming convention: vpnbot-<service>
- Rollback/cleanup on failure

Architecture note: SSHManager is an async context manager that holds one persistent
connection for the duration of an operation. Installers wrap their install() and
discover_existing() bodies in ``async with self.ssh:`` so all sequential SSH commands
reuse a single connection (connect_timeout and per-command timeout apply). One-off
run_command() calls used outside a context still open and close a connection per command.
"""

import secrets
import string
import time
from collections.abc import Awaitable, Callable

import asyncssh
from loguru import logger

from app.services.ssh_service import SSHManager
from app.services.vpn_providers.port_manager import PortManager

ProgressCallback = Callable[[str], Awaitable[None]]


class AlreadyInstalledError(RuntimeError):
    """Raised when a vpnbot container already exists on the server."""

CONTAINER_PREFIX = "vpnbot"
BASE_DIR = "/opt/vpnbot"
PREPARED_MARKER = f"{BASE_DIR}/.prepared"
FIREWALL_POLICY_FILE = f"{BASE_DIR}/.firewall-policy"


def _container_name(service: str) -> str:
    return f"{CONTAINER_PREFIX}-{service}"


class BaseInstaller:
    """Base class for all VPN service installers."""

    SERVICE_NAME: str = ""

    # ── Container identification ──────────────────────────────────────

    def __init__(
        self,
        ssh: SSHManager,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.ssh = ssh
        self.port_manager = PortManager(ssh)
        self._use_sudo = False
        self._progress_callback = progress_callback
        self._last_progress_ts: float = 0.0

    async def _progress(self, step: int, total: int, text: str) -> None:
        """Report installation progress.

        Rate-limited to 1 call per second to avoid Telegram API limits.
        The final step (step == total) always bypasses rate limiting.

        Args:
            step: Current step number (1-based).
            total: Total number of steps.
            text: Human-readable description of current step.
        """
        if self._progress_callback is None:
            return

        now = time.monotonic()
        if now - self._last_progress_ts < 1.0 and step < total:
            return
        self._last_progress_ts = now

        message = f"[{step}/{total}] {text}"
        try:
            await self._progress_callback(message)
        except Exception as e:
            from aiogram.exceptions import TelegramAPIError

            if not isinstance(e, TelegramAPIError):
                raise
            logger.debug("Ошибка progress callback (подавлена): {}", e)

    async def _cmd(self, command: str, input_data: str | None = None) -> str:
        """Execute command with optional sudo prefix."""
        if self._use_sudo:
            import shlex
            command = f"sudo bash -c {shlex.quote(command)}"
        return await self.ssh.run_command(command, input_data=input_data)

    async def _write_file(self, filepath: str, content: str) -> None:
        """Write file with optional sudo."""
        if self._use_sudo:
            await self.ssh.run_command(
                f"sudo tee {filepath} > /dev/null", input_data=content
            )
        else:
            await self.ssh.write_file(filepath, content)

    async def preflight_check(self) -> tuple[bool, str]:
        """Run pre-installation checks: TCP probe + SSH + root/sudo access.

        Returns:
            Tuple of (success: bool, message: str).
        """
        from app.services.server_monitor import ServerMonitor

        host = self.ssh.host
        port = self.ssh.port or 22

        # TCP-проба того же порта, на который пойдёт SSH (ICMP хостеры часто
        # режут — проба на нём ломала установку на живых серверах).
        tcp_ok = await ServerMonitor.check_tcp(host, port)
        if not tcp_ok:
            return False, f"Сервер {host}:{port} недоступен (TCP-проба)"

        ssh_ok = await self.ssh.test_connection()
        if not ssh_ok:
            return False, f"Не удалось подключиться по SSH к {host}:{port}. Проверьте логин/пароль/ключ."

        root_ok, root_msg = await self._check_root_access()
        if not root_ok:
            return False, root_msg

        return True, "OK"

    async def _check_root_access(self) -> tuple[bool, str]:
        """Check if SSH user is root or has passwordless sudo.

        Returns:
            Tuple of (has_root: bool, message: str).
        """
        try:
            result = await self.ssh.run_command("whoami")
            if result.strip() == "root":
                self._use_sudo = False
                return True, "root"
        except Exception:
            pass

        try:
            result = await self.ssh.run_command("sudo -n whoami 2>/dev/null")
            if result.strip() == "root":
                self._use_sudo = True
                return True, "sudo"
        except Exception:
            pass

        return False, (
            f"Пользователь {self.ssh.username}@{self.ssh.host} "
            "не имеет root-прав и не настроен passwordless sudo. "
            "Подключитесь под root или настройте sudo."
        )

    async def check_already_installed(self) -> bool:
        """Check if a container with our naming convention already exists."""
        name = _container_name(self.SERVICE_NAME)
        try:
            result = await self._cmd(
                f"docker ps -a --filter name=^{name}$ --format '{{{{.Names}}}}'"
            )
            return bool(result.strip())
        except Exception as e:
            # Re-raise SSH connection errors so the UI can catch them
            import asyncssh
            if isinstance(e, (asyncssh.Error, OSError)) or "SSH" in str(e):
                raise
            return False

    async def list_installed_services(self) -> list[str]:
        """List all vpnbot-managed containers on the server."""
        try:
            result = await self._cmd(
                f"docker ps -a --filter name={CONTAINER_PREFIX}- --format '{{{{.Names}}}}'"
            )
            names = [n.strip() for n in result.strip().split("\n") if n.strip()]
            return [n.removeprefix(f"{CONTAINER_PREFIX}-") for n in names]
        except Exception:
            return []

    # ── Host preparation ──────────────────────────────────────────────

    async def is_prepared(self) -> bool:
        """Check if host has already been prepared."""
        try:
            result = await self._cmd(f"test -f {PREPARED_MARKER} && echo yes")
            return result.strip() == "yes"
        except Exception as e:
            # Re-raise SSH connection errors so the UI can catch them
            import asyncssh
            if isinstance(e, (asyncssh.Error, OSError)) or "SSH" in str(e):
                raise
            return False

    async def prepare_host(self) -> None:
        """Prepare server: install Docker, utils, configure UFW.

        Always verifies Docker is available even if .prepared marker exists,
        because Docker may have been removed or become inaccessible.
        """
        logger.info("Подготовка хоста {}...", self.ssh.host)

        if not await self._docker_is_available():
            logger.info("Docker не найден, устанавливаю...")
            await self._ensure_docker()
        elif not await self._compose_is_available():
            logger.info("Docker найден, но плагин compose v2 отсутствует, устанавливаю...")
            await self._install_compose_plugin()

        if await self.is_prepared():
            logger.info("Хост уже подготовлен ({} существует), пропускаю utils/UFW", PREPARED_MARKER)
            return

        await self._ensure_utils()
        await self._ensure_ufw()
        await self._mark_prepared()
        logger.info("Хост {} подготовлен", self.ssh.host)

    async def _detect_os(self) -> str:
        """Detect the OS package manager. Returns: debian, fedora, centos, opensuse, archlinux."""
        script = (
            "if which apt-get > /dev/null 2>&1; then echo debian;"
            "elif which dnf > /dev/null 2>&1; then echo fedora;"
            "elif which yum > /dev/null 2>&1; then echo centos;"
            "elif which zypper > /dev/null 2>&1; then echo opensuse;"
            "elif which pacman > /dev/null 2>&1; then echo archlinux;"
            "else echo unknown; fi"
        )
        dist = await self._cmd(script)
        dist = dist.strip()
        logger.info("Семейство ОС: {}", dist)
        return dist

    async def _ensure_docker(self) -> None:
        """Install Docker CE with compose v2 plugin if not present.

        Uses official Docker convenience script (get.docker.com) which:
        - Installs from Docker's official repos (latest stable)
        - Includes docker compose v2 plugin
        - Works on all major Linux distros

        After install verifies both `docker` and `docker compose` are functional.
        """
        if await self._docker_is_available():
            logger.info("Docker уже доступен")
            await self._ensure_docker_active()
            if not await self._compose_is_available():
                logger.info("Плагин docker compose v2 не найден, устанавливаю")
                await self._install_compose_plugin()
            return

        logger.info("Docker не найден, устанавливаю через get.docker.com...")
        await self._cmd(
            "curl -fsSL https://get.docker.com | sh"
        )
        await self._cmd("systemctl enable --now docker")
        await self._cmd("sleep 3")
        await self._fix_docker_path()
        await self._ensure_docker_active()

        docker_ver = await self._cmd("docker --version")
        logger.info("Docker установлен: {}", docker_ver)

        if not await self._compose_is_available():
            await self._install_compose_plugin()

        compose_ver = await self._cmd("docker compose version")
        logger.info("Docker Compose: {}", compose_ver)

    async def _docker_is_available(self) -> bool:
        """Check if docker command is accessible in PATH."""
        try:
            result = await self._cmd("docker --version")
            return bool(result.strip())
        except Exception as e:
            # Re-raise SSH connection errors so the UI can catch them
            import asyncssh
            if isinstance(e, (asyncssh.Error, OSError)) or "SSH" in str(e):
                raise
            return False

    async def _compose_is_available(self) -> bool:
        """Check if docker compose v2 plugin is installed."""
        try:
            result = await self._cmd("docker compose version")
            return bool(result.strip())
        except Exception as e:
            # Re-raise SSH connection errors so the UI can catch them
            import asyncssh
            if isinstance(e, (asyncssh.Error, OSError)) or "SSH" in str(e):
                raise
            return False

    async def _install_compose_plugin(self) -> None:
        """Install docker compose v2 plugin via package manager or direct binary download."""
        dist = await self._detect_os()

        pkg_cmds = {
            "debian": "apt-get update -yq && apt-get install -yq docker-compose-plugin",
            "fedora": "dnf install -yq docker-compose-plugin",
            "centos": "yum install -yq docker-compose-plugin",
            "opensuse": "zypper -nq install docker-compose-plugin",
            "archlinux": "pacman -S --noconfirm --noprogressbar docker-compose",
        }

        cmd = pkg_cmds.get(dist)
        if cmd:
            try:
                await self._cmd(cmd)
                if await self._compose_is_available():
                    return
            except Exception:
                logger.warning("Установка через пакетный менеджер не удалась (dist={}), пробую бинарный download", dist)

        await self._cmd(
            "mkdir -p /usr/local/lib/docker/cli-plugins && "
            "curl -SL "
            "https://github.com/docker/compose/releases/latest/download/"
            "docker-compose-linux-x86_64 "
            "-o /usr/local/lib/docker/cli-plugins/docker-compose && "
            "chmod +x /usr/local/lib/docker/cli-plugins/docker-compose"
        )

        if not await self._compose_is_available():
            raise RuntimeError(
                "Docker compose v2 plugin installation failed. "
                "Manual intervention required."
            )

    async def _fix_docker_path(self) -> None:
        """Fix Docker not in PATH by creating symlinks for common locations."""
        fix_script = (
            'docker_path=$(command -v docker 2>/dev/null); '
            'if [ -z "$docker_path" ]; then '
            '  for candidate in /usr/bin/docker /usr/local/bin/docker /snap/bin/docker '
            '                   /usr/libexec/docker/docker; do '
            '    if [ -x "$candidate" ]; then '
            '      ln -sf "$candidate" /usr/local/bin/docker 2>/dev/null; '
            '      ln -sf "$candidate" /usr/bin/docker 2>/dev/null; '
            '      break; '
            '    fi; '
            '  done; '
            'fi'
        )
        try:
            await self._cmd(fix_script)
        except Exception as e:
            logger.warning("Попытка исправить PATH для Docker, возможны проблемы: {}", e)

    async def _ensure_docker_active(self) -> None:
        """Ensure Docker daemon is running."""
        await self._cmd(
            "if [ \"$(systemctl is-active docker 2>/dev/null)\" != \"active\" ]; then "
            "systemctl start docker; sleep 3; "
            "fi"
        )

    async def _ensure_utils(self) -> None:
        """Install essential utilities (curl, jq)."""
        dist = await self._detect_os()
        if dist == "debian":
            await self._cmd(
                "apt-get update -yq && apt-get install -yq curl jq"
            )
        elif dist == "fedora":
            await self._cmd("dnf install -yq curl jq")
        elif dist == "centos":
            await self._cmd("yum install -yq curl jq")
        elif dist == "opensuse":
            await self._cmd("zypper -nq install curl jq")
        elif dist == "archlinux":
            await self._cmd("pacman -S --noconfirm --noprogressbar curl jq")
        else:
            logger.warning("Неизвестный дистрибутив '{}', пропускаю установку утилит", dist)

    async def _ensure_ufw(self) -> None:
        """Install and enable UFW with SSH allowed."""
        dist = await self._detect_os()
        if dist == "debian":
            await self._cmd("apt-get install -yq ufw")
        elif dist in ("fedora", "centos"):
            await self._cmd("yum install -yq ufw || dnf install -yq ufw")
        elif dist == "opensuse":
            await self._cmd("zypper -nq install ufw")
        elif dist == "archlinux":
            await self._cmd("pacman -S --noconfirm ufw")

        ssh_port = self.ssh.port
        await self._cmd(
            f"ufw allow {ssh_port}/tcp && ufw --force enable"
        )
        logger.info("UFW включён, SSH-порт {} открыт", ssh_port)

    async def _mark_prepared(self) -> None:
        """Mark host as prepared."""
        await self._cmd(f"mkdir -p {BASE_DIR} && touch {PREPARED_MARKER}")

    async def get_firewall_policy(self) -> str | None:
        """Read firewall policy from server. Returns 'strict', 'permissive', or None."""
        try:
            result = await self._cmd(f"cat {FIREWALL_POLICY_FILE} 2>/dev/null")
            policy = result.strip()
            return policy if policy in ("strict", "permissive") else None
        except Exception:
            return None

    async def apply_firewall_policy(self, strict: bool) -> None:
        """Apply firewall policy. Only called on first server setup.

        Args:
            strict: If True, deny all incoming except SSH. If False, leave as-is.
        """
        ssh_port = self.ssh.port

        if strict:
            await self._cmd(
                f"ufw default deny incoming && "
                f"ufw default allow outgoing && "
                f"ufw allow {ssh_port}/tcp && "
                f"ufw --force reload"
            )
            logger.info("Строгий файрвол на {}: разрешён только SSH/{}", self.ssh.host, ssh_port)
        else:
            logger.info("Мягкий файрвол на {}: политика UFW не изменена", self.ssh.host)

        policy = "strict" if strict else "permissive"
        await self._cmd(
            f"mkdir -p {BASE_DIR} && echo '{policy}' > {FIREWALL_POLICY_FILE}"
        )

    # ── Cleanup / Rollback ────────────────────────────────────────────

    async def cleanup(
        self,
        dirs: list[str] | None = None,
        ports: list[tuple[int, str]] | None = None,
    ) -> None:
        """Remove container, directories, and close UFW ports for rollback.

        Args:
            dirs: List of remote directories to remove.
            ports: List of (port, protocol) tuples to close in UFW.
        """
        name = _container_name(self.SERVICE_NAME)
        logger.warning("Откат: очистка контейнера {}", name)

        try:
            await self._cmd(f"docker rm -f {name} 2>/dev/null || true")
        except Exception as e:
            logger.error("Не удалось удалить контейнер {}: {}", name, e)

        for d in dirs or []:
            try:
                await self._cmd(f"rm -rf {d}")
            except Exception as e:
                logger.error("Не удалось удалить директорию {}: {}", d, e)

        for port, proto in ports or []:
            try:
                await self.port_manager.close_port(port, proto)
            except Exception as e:
                logger.error("Не удалось закрыть порт {}/{}: {}", port, proto, e)

    # ── Helpers ───────────────────────────────────────────────────────

    async def ensure_dirs(self, *paths: str) -> None:
        """Create directories on the remote server."""
        for p in paths:
            await self._cmd(f"mkdir -p {p}")

    @staticmethod
    def generate_random_string(length: int = 16) -> str:
        chars = string.ascii_letters + string.digits
        return "".join(secrets.choice(chars) for _ in range(length))

    async def check_port_free(self, port: int) -> bool:
        """Check if a specific port is free on the server (TCP + UDP)."""
        return await self.port_manager.is_port_free(port)

    # ── SSH port change ────────────────────────────────────────────────

    async def _test_ssh_on_port(self, port: int) -> bool:
        """Test SSH connectivity on a specific port using the same credentials.

        Uses asyncssh directly to avoid SSHManager.__new__() hacks.
        """
        password = self.ssh.get_ssh_password()
        key_data = self.ssh.get_ssh_key()
        client_keys = None
        if key_data:
            client_keys = [asyncssh.import_private_key(key_data)]
        try:
            async with await asyncssh.connect(
                self.ssh.host,
                port=port,
                username=self.ssh.username,
                password=password,
                client_keys=client_keys,
                known_hosts=None,
            ):
                return True
        except Exception as e:
            logger.error("Проверка SSH на порту {} не удалась: {}", port, e)
            return False

    async def change_ssh_port(self, new_port: int) -> tuple[bool, str]:
        """Change SSH daemon port with automatic rollback on failure.

        Strategy:
        1. Add new port alongside old port in sshd_config
        2. Open new port in UFW
        3. Restart sshd
        4. Verify connectivity on new port
        5. If OK: remove old port, close old port in UFW
        6. If FAIL: revert config, keep old port

        Args:
            new_port: New SSH port number.

        Returns:
            Tuple of (success, message).
        """
        old_port = self.ssh.port
        if new_port == old_port:
            return True, f"SSH port already {old_port}"

        host = self.ssh.host

        if not await self.check_port_free(new_port):
            return False, f"Порт {new_port} занят на сервере"

        config_backup = await self._cmd("cat /etc/ssh/sshd_config")

        try:
            await self._cmd(
                f"sed -i 's/^#*Port {old_port}/Port {old_port}\\nPort {new_port}/' /etc/ssh/sshd_config"
            )

            if f"Port {new_port}" not in await self._cmd("grep '^Port' /etc/ssh/sshd_config"):
                await self._cmd(
                    f"echo 'Port {old_port}' >> /etc/ssh/sshd_config && "
                    f"echo 'Port {new_port}' >> /etc/ssh/sshd_config"
                )

            await self._cmd(f"ufw allow {new_port}/tcp")
            await self._cmd("systemctl restart sshd || systemctl restart ssh")
            import asyncio
            await asyncio.sleep(3)

            if await self._test_ssh_on_port(new_port):
                await self._cmd(
                    f"sed -i '/^Port {old_port}$/d' /etc/ssh/sshd_config"
                )
                await self._cmd(f"ufw delete allow {old_port}/tcp || true")
                await self._cmd("systemctl restart sshd || systemctl restart ssh")

                logger.info("SSH-порт изменён: {} -> {} на {}", old_port, new_port, host)
                return True, f"SSH порт изменён: {old_port} → {new_port}"
            else:
                raise RuntimeError("Не удалось подключиться на новый порт")

        except Exception as e:
            logger.error("Смена SSH-порта не удалась, откат: {}", e)
            try:
                await self._write_file("/etc/ssh/sshd_config", config_backup)
                await self._cmd("systemctl restart sshd || systemctl restart ssh")
                await self._cmd(f"ufw delete allow {new_port}/tcp || true")
            except Exception as rollback_err:
                logger.error("Откат тоже не удался: {}", rollback_err)
                return False, f"Ошибка смены порта и отката: {rollback_err}"

            return False, f"Не удалось сменить SSH порт: {e}. Откачено к порту {old_port}."

    # ── Healthcheck ────────────────────────────────────────────────────

    async def healthcheck(self, service_name: str) -> tuple[bool, str]:
        """Check if a vpnbot container is running and healthy.

        Args:
            service_name: Service name (e.g. 'awg', 'xui').

        Returns:
            Tuple of (healthy, status_text).
        """
        container = _container_name(service_name)
        try:
            status = await self._cmd(
                f"docker inspect --format '{{{{.State.Status}}}}' {container} 2>/dev/null"
            )
            status = status.strip().strip("'")

            if status == "running":
                uptime = await self._cmd(
                    f"docker inspect --format '{{{{.State.StartedAt}}}}' {container} 2>/dev/null"
                )
                return True, f"running (started {uptime.strip().strip(chr(39))})"

            return False, f"container status: {status}"
        except Exception as e:
            return False, f"error: {e}"
