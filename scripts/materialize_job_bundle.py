from __future__ import annotations

import argparse
import sys
from typing import Sequence

from sqlalchemy.orm import Session, sessionmaker

from app.db import SessionLocal
from app.services.job_asset_bundles import get_latest_job_asset_bundle_by_job_id
from app.services.job_bundle_materialization import (
    JobBundleMaterializationError,
    materialize_job_asset_bundle,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize a Job Asset Bundle")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--bundle-uid", help="Job Asset Bundle UID to materialize")
    selector.add_argument("--job-id", help="Materialize the latest bundle for this job id")
    parser.add_argument("--force", action="store_true", help="Recreate files and manifest")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    session_factory: sessionmaker[Session] = SessionLocal,
) -> int:
    args = parse_args(argv)
    with session_factory() as session:
        try:
            bundle_uid = args.bundle_uid
            if args.job_id:
                bundle = get_latest_job_asset_bundle_by_job_id(session, args.job_id)
                if bundle is None:
                    print("Bundle not found", file=sys.stderr)
                    return 1
                bundle_uid = bundle.bundle_uid
            result = materialize_job_asset_bundle(
                session,
                bundle_uid,
                force=args.force,
            )
            session.commit()
        except JobBundleMaterializationError as exc:
            session.rollback()
            print(str(exc), file=sys.stderr)
            return 1
        except Exception:
            session.rollback()
            print("Materialization failed", file=sys.stderr)
            return 1

    manifest_path = None
    if result["materialized_assets_dir"]:
        manifest_path = (
            f"/data/{result['materialized_assets_dir']}/manifests/renderer-manifest.json"
        )
    print(f"bundle_uid={result['bundle_uid']}")
    print(f"job_id={result['job_id']}")
    print(f"total_assets={result['total_assets']}")
    print(f"materialized_assets={result['materialized_assets']}")
    print(f"failed_assets={result['failed_assets']}")
    print(f"materialization_status={result['materialization_status']}")
    print(f"renderer_manifest_path={manifest_path or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
