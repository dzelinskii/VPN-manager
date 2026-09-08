"""Сервис обработки расхождений БД ↔ XUI-панель.

Расхождение возникает, когда состояние панели не совпадает с БД:
- missing_on_panel — соединение есть в БД, но клиента нет на панели (ручное удаление);
- extra_on_panel — клиент/привязка есть на панели, но нет соответствующей строки БД
  (ручное добавление).

Вместо немедленного деструктива реконсайлер в режиме ask откладывает расхождение
сюда (`record_findings`) и спрашивает админа. Решение применяется через `resolve`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select

from app.database.models import PendingDivergence

KIND_MISSING = "missing_on_panel"  # в БД есть, на панели нет
KIND_EXTRA = "extra_on_panel"  # на панели есть, в БД нет

STATUS_OPEN = "open"
STATUS_APPLIED = "applied"  # выполнен деструктив (как сделал бы авто)
STATUS_ADOPTED = "adopted"  # ручное изменение сохранено (adopt/restore)
STATUS_IGNORED = "ignored"
STATUS_OBSOLETE = "obsolete"  # расхождение исчезло само до решения

DECISION_APPLY = "apply"  # 🗑 деструктив
DECISION_SAVE = "save"  # 💾 сохранить ручное (adopt для extra / restore для missing)
DECISION_IGNORE = "ignore"  # 💤


@dataclass
class DivergenceFinding:
    """Найденное за проход расхождение (ещё не записанное в БД)."""

    server_id: int
    kind: str
    email: str
    subscription_id: int | None
    details: dict


def _ms_to_dt(ms: int | None) -> datetime | None:
    """epoch-миллисекунды → tz-aware UTC datetime (или None)."""
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


class DivergenceService:
    """Детект, дедуп и разрешение расхождений."""

    def __init__(self, session) -> None:
        self.session = session

    async def record_findings(
        self, findings: list[DivergenceFinding], batch_id: str
    ) -> list[PendingDivergence]:
        """Записать новые расхождения, дедуплицируя по (server_id, kind, email)
        среди открытых. Вернуть только созданные (для отправки дайджеста)."""
        created: list[PendingDivergence] = []
        for f in findings:
            existing = (
                await self.session.execute(
                    select(PendingDivergence).where(
                        PendingDivergence.server_id == f.server_id,
                        PendingDivergence.kind == f.kind,
                        PendingDivergence.email == f.email,
                        PendingDivergence.status == STATUS_OPEN,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue
            pd = PendingDivergence(
                server_id=f.server_id,
                kind=f.kind,
                email=f.email,
                subscription_id=f.subscription_id,
                details_json=f.details or {},
                status=STATUS_OPEN,
                batch_id=batch_id,
            )
            self.session.add(pd)
            created.append(pd)
        await self.session.flush()
        return created

    async def mark_obsolete(
        self, server_id: int, present_keys: set[tuple[str, str]]
    ) -> list[PendingDivergence]:
        """Перевести open-расхождения сервера, которых больше нет в проходе, в obsolete.

        present_keys — множество (kind, email), фактически наблюдаемых в текущем снимке.
        """
        rows = (
            await self.session.execute(
                select(PendingDivergence).where(
                    PendingDivergence.server_id == server_id,
                    PendingDivergence.status == STATUS_OPEN,
                )
            )
        ).scalars().all()
        affected: list[PendingDivergence] = []
        now = datetime.now(UTC)
        for pd in rows:
            if (pd.kind, pd.email) not in present_keys:
                pd.status = STATUS_OBSOLETE
                pd.resolved_at = now
                affected.append(pd)
        if affected:
            await self.session.flush()
        return affected

    async def list_open_for_batch(self, batch_id: str) -> list[PendingDivergence]:
        """Открытые расхождения батча (для разбора по одному / групповых действий)."""
        rows = (
            await self.session.execute(
                select(PendingDivergence)
                .where(
                    PendingDivergence.batch_id == batch_id,
                    PendingDivergence.status == STATUS_OPEN,
                )
                .order_by(PendingDivergence.id)
            )
        ).scalars().all()
        return list(rows)

    async def list_for_batch(self, batch_id: str) -> list[PendingDivergence]:
        """Все расхождения батча (любой статус) — для построения дайджеста."""
        rows = (
            await self.session.execute(
                select(PendingDivergence)
                .where(PendingDivergence.batch_id == batch_id)
                .order_by(PendingDivergence.id)
            )
        ).scalars().all()
        return list(rows)

    # --- разрешение -------------------------------------------------------

    async def resolve(
        self,
        pending_id: int,
        decision: str,
        resolved_by: int | None,
        xui_client=None,
    ) -> PendingDivergence | None:
        """Разрешить расхождение. Идемпотентно: не-open вернётся без изменений.

        Раскладка (kind × decision):
        - missing + apply  → удалить строки БД (деструктив, как авто);
        - missing + save   → restore: пересоздать клиента на панели (нужен xui_client);
        - extra   + apply  → панель: detach orphan / delete зомби (нужен xui_client);
        - extra   + save   → adopt: создать строки БД под факт панели;
        - *       + ignore → оставить как есть.
        """
        pd = await self.session.get(PendingDivergence, pending_id)
        if pd is None or pd.status != STATUS_OPEN:
            return pd

        if decision == DECISION_IGNORE:
            new_status = STATUS_IGNORED
        elif pd.kind == KIND_MISSING:
            if decision == DECISION_APPLY:
                await self._delete_db_rows(pd)
                new_status = STATUS_APPLIED
            else:
                await self._restore_on_panel(pd, xui_client)
                new_status = STATUS_ADOPTED
        elif pd.kind == KIND_EXTRA:
            if decision == DECISION_APPLY:
                await self._remove_on_panel(pd, xui_client)
                new_status = STATUS_APPLIED
            else:
                await self._adopt_into_db(pd)
                new_status = STATUS_ADOPTED
        else:
            logger.warning("Неизвестный kind расхождения {}: {}", pd.id, pd.kind)
            return pd

        pd.status = new_status
        pd.resolved_at = datetime.now(UTC)
        pd.resolved_by = resolved_by
        await self.session.flush()
        return pd

    async def _delete_db_rows(self, pd: PendingDivergence) -> None:
        """missing + apply: удалить строки БД, чьего клиента нет на панели."""
        from app.database.models import XUIInboundConnection

        ids = pd.details_json.get("inbound_db_ids") or []
        conditions = [XUIInboundConnection.email == pd.email]
        if pd.subscription_id is not None:
            conditions.append(XUIInboundConnection.subscription_id == pd.subscription_id)
        if ids:
            conditions.append(XUIInboundConnection.inbound_id.in_(ids))
        rows = (
            await self.session.execute(select(XUIInboundConnection).where(*conditions))
        ).scalars().all()
        for r in rows:
            await self.session.delete(r)

    async def _adopt_into_db(self, pd: PendingDivergence) -> None:
        """extra + save: создать строки БД под фактические привязки панели."""
        from app.database.models import XUIInboundConnection

        d = pd.details_json or {}
        ids = d.get("inbound_db_ids") or []
        expiry = _ms_to_dt(d.get("expiry_ms"))
        for inbound_db_id in ids:
            exists = (
                await self.session.execute(
                    select(XUIInboundConnection).where(
                        XUIInboundConnection.subscription_id == pd.subscription_id,
                        XUIInboundConnection.inbound_id == inbound_db_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is not None:
                continue
            self.session.add(
                XUIInboundConnection(
                    subscription_id=pd.subscription_id,
                    inbound_id=inbound_db_id,
                    is_enabled=bool(d.get("enable", True)),
                    total_gb=int(d.get("total_gb", 0) or 0),
                    expiry_date=expiry,
                    sync_status="synced",
                    last_sync_at=datetime.now(UTC),
                    provider_payload=d.get("panel_payload"),
                    uuid=d.get("uuid"),
                    email=pd.email,
                    xui_client_id=d.get("uuid"),
                )
            )

    async def _remove_on_panel(self, pd: PendingDivergence, xui_client) -> None:
        """extra + apply: detach осиротевших привязок или delete зомби целиком."""
        if xui_client is None:
            raise ValueError("xui_client требуется для операции на панели")
        d = pd.details_json or {}
        orphan = d.get("orphan_xui_ids") or []
        if d.get("has_valid") and orphan:
            await xui_client.detach_client(pd.email, orphan)
        else:
            await xui_client.delete_client(pd.email)

    async def _restore_on_panel(self, pd: PendingDivergence, xui_client) -> None:
        """missing + save: пересоздать клиента на панели из данных БД/details."""
        if xui_client is None:
            raise ValueError("xui_client требуется для восстановления на панели")
        from app.database.models import XUIInboundConnection
        from app.xui_client.models import XUIAddClientRequest

        d = pd.details_json or {}
        req = XUIAddClientRequest(
            id=d.get("uuid") or "",
            email=pd.email,
            enable=bool(d.get("enable", True)),
            flow=d.get("flow", "xtls-rprx-vision"),
            totalGB=int(d.get("total_gb", 0) or 0) * 1024 * 1024 * 1024,
            expiryTime=int(d.get("expiry_ms") or 0),
            subId=d.get("subscription_token", "") or "",
            tgId=int(d.get("tg_id", 0) or 0),
        )
        await xui_client.add_client(req, d.get("inbound_xui_ids") or [])
        rows = (
            await self.session.execute(
                select(XUIInboundConnection).where(
                    XUIInboundConnection.subscription_id == pd.subscription_id,
                    XUIInboundConnection.email == pd.email,
                )
            )
        ).scalars().all()
        for r in rows:
            # Соседняя строка может ждать отправки собственного изменения на
            # другой inbound — восстановление этого клиента её не применило,
            # и снимать признак нельзя.
            if r.sync_status != "pending_push":
                r.sync_status = "synced"
