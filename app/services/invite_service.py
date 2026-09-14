from __future__ import annotations

import secrets
from datetime import datetime
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import config
from app.models import InviteCode, utcnow


def generate_random_invite_code(prefix: str = "INV-", length: int = 8) -> str:
    """生成易于复制辨识的随机邀请码（如 INV-7K9XM24P）。"""
    alphabet = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    random_part = "".join(secrets.choice(alphabet) for _ in range(length))
    return f"{prefix}{random_part}"


def is_invite_code_required() -> bool:
    """检查当前系统是否强制开启了注册邀请码验证。"""
    return config.get_settings().invite_required


def validate_and_consume_invite_code(db: Session, code_str: str) -> tuple[bool, str]:
    """验证并消耗一个邀请码。

    返回值：(是否有效, 提示信息或消耗来源)。
    """
    settings = config.get_settings()
    cleaned = code_str.strip()

    # 如果系统未开启邀请码验证
    if not settings.invite_required:
        return True, "not_required"

    if not cleaned:
        return False, "本系统已开启邀请注册，请输入邀请码"

    # 1. 优先校验环境变量预设通用邀请码（逗号分隔，大小写不敏感）
    if settings.registration_invite_codes:
        env_codes = [c.strip().upper() for c in settings.registration_invite_codes.split(",") if c.strip()]
        if cleaned.upper() in env_codes:
            return True, "env_preset"

    # 2. 校验数据库邀请码表
    stmt = select(InviteCode).where(
        or_(
            InviteCode.code == cleaned,
            InviteCode.code == cleaned.upper(),
        )
    )
    invite = db.scalar(stmt)
    if not invite:
        return False, "邀请码无效或不存在"
    if invite.status == "disabled":
        return False, "邀请码已被停用"
    if invite.status == "depleted":
        return False, "邀请码已达最大使用次数"
    if invite.status == "expired":
        return False, "邀请码已过期"
    if invite.status != "active":
        return False, "邀请码无效或已被停用"

    now = utcnow()
    if invite.expires_at and invite.expires_at < now:
        invite.status = "expired"
        db.commit()
        return False, "邀请码已过期"

    if invite.max_uses != -1 and invite.used_count >= invite.max_uses:
        invite.status = "depleted"
        db.commit()
        return False, "邀请码已达最大使用次数"

    # 消耗并更新计数
    invite.used_count += 1
    if invite.max_uses != -1 and invite.used_count >= invite.max_uses:
        invite.status = "depleted"
    db.commit()

    return True, f"db:{invite.id}"


def create_invite_code(
    db: Session,
    code: str | None = None,
    max_uses: int = 1,
    note: str = "",
    expires_at: datetime | None = None,
    creator_id: str | None = None,
) -> InviteCode:
    """创建新邀请码。"""
    final_code = (code or "").strip().upper() or generate_random_invite_code()
    invite = InviteCode(
        id=str(uuid4()),
        code=final_code,
        max_uses=max_uses,
        used_count=0,
        status="active",
        note=note.strip(),
        created_by=creator_id,
        created_at=utcnow(),
        expires_at=expires_at,
    )
    db.add(invite)
    db.commit()
    return invite


def list_invite_codes(db: Session, limit: int = 50) -> list[InviteCode]:
    """获取所有邀请码列表。"""
    return list(db.scalars(select(InviteCode).order_by(InviteCode.created_at.desc()).limit(limit)).all())


def disable_invite_code(db: Session, invite_id: str) -> bool:
    """停用/作废邀请码。"""
    invite = db.get(InviteCode, invite_id)
    if not invite:
        return False
    invite.status = "disabled"
    db.commit()
    return True
