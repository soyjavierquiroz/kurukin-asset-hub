from sqlalchemy import CheckConstraint, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import TimestampMixin

JOB_ASSET_BUNDLE_STATUS_VALUES = ("prepared", "failed", "cleaned")


class JobAssetBundle(TimestampMixin, Base):
    __tablename__ = "job_asset_bundles"
    __table_args__ = (
        CheckConstraint(
            f"status in {JOB_ASSET_BUNDLE_STATUS_VALUES}",
            name="ck_job_asset_bundles_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="prepared",
        server_default=text("'prepared'"),
    )
    output_dir: Mapped[str] = mapped_column(String(1200), nullable=False)
    manifest_path: Mapped[str | None] = mapped_column(String(1200))
