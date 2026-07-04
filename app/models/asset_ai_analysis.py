from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.asset import Asset


class AssetAIAnalysis(Base):
    __tablename__ = "asset_ai_analyses"
    __table_args__ = (
        Index("ix_asset_ai_analyses_asset_id", "asset_id"),
        Index("ix_asset_ai_analyses_provider", "provider"),
        Index("ix_asset_ai_analyses_model", "model"),
        Index("ix_asset_ai_analyses_prompt_version", "prompt_version"),
        Index("ix_asset_ai_analyses_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="openai",
        server_default=text("'openai'"),
    )
    input_type: Mapped[str] = mapped_column(String(40), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    asset: Mapped["Asset"] = relationship(back_populates="ai_analyses")
