from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Index, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import PROVIDER_VALUES, TimestampMixin

if TYPE_CHECKING:
    from app.models.source import Source

AUTH_TYPE_VALUES = ("rclone", "oauth", "service_account", "other")


class AuthProfile(TimestampMixin, Base):
    __tablename__ = "auth_profiles"
    __table_args__ = (
        CheckConstraint(
            f"provider in {PROVIDER_VALUES}",
            name="ck_auth_profiles_provider",
        ),
        CheckConstraint(
            f"auth_type in {AUTH_TYPE_VALUES}",
            name="ck_auth_profiles_auth_type",
        ),
        Index("ix_auth_profiles_provider", "provider"),
        Index("ix_auth_profiles_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    auth_profile_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())

    sources: Mapped[list["Source"]] = relationship(back_populates="auth_profile")
