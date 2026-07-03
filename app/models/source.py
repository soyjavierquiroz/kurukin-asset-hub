from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.common import PROVIDER_VALUES, TimestampMixin

if TYPE_CHECKING:
    from app.models.asset import Asset
    from app.models.auth_profile import AuthProfile


class Source(TimestampMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            f"provider in {PROVIDER_VALUES}",
            name="ck_sources_provider",
        ),
        Index("ix_sources_provider", "provider"),
        Index("ix_sources_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    auth_profile_id: Mapped[int | None] = mapped_column(ForeignKey("auth_profiles.id"))
    rclone_remote: Mapped[str | None] = mapped_column(String(255))
    root_path: Mapped[str | None] = mapped_column(String(1000))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())

    auth_profile: Mapped["AuthProfile | None"] = relationship(back_populates="sources")
    assets: Mapped[list["Asset"]] = relationship(back_populates="source")

