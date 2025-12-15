from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import update

from . import db
from .models import GlobalConfig

DONATION_GOAL_CENTS = 200_00
CONFIG_SINGLETON_ID = 1


def _ensure_config_row() -> GlobalConfig:
    config = db.session.get(GlobalConfig, CONFIG_SINGLETON_ID)
    if config is None:
        config = GlobalConfig(id=CONFIG_SINGLETON_ID, total_donated_cents=0)
        db.session.add(config)
        db.session.commit()
    return config


def get_global_config() -> GlobalConfig:
    return _ensure_config_row()


def increment_total_donated(amount_cents: int) -> GlobalConfig:
    if amount_cents <= 0:
        return _ensure_config_row()

    stmt = (
        update(GlobalConfig)
        .where(GlobalConfig.id == CONFIG_SINGLETON_ID)
        .values(
            total_donated_cents=GlobalConfig.total_donated_cents + amount_cents,
        )
    )
    result = db.session.execute(stmt)
    if result.rowcount == 0:
        config = GlobalConfig(
            id=CONFIG_SINGLETON_ID,
            total_donated_cents=amount_cents,
        )
        db.session.add(config)
    db.session.commit()
    return _ensure_config_row()


def parse_amount_to_cents(amount: str | None) -> int:
    if not amount:
        return 0
    try:
        value = Decimal(amount)
    except InvalidOperation:
        return 0
    cents = (value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)
