from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from sqlalchemy.orm import Session, sessionmaker

from app.db import SessionLocal
from app.services.job_asset_bundles import get_latest_job_asset_bundle_by_job_id
from app.services.job_bundle_materialization import load_bundle
from app.services.renderer_manifest import (
    build_renderer_manifest,
    validate_renderer_manifest_contract,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print a renderer manifest JSON contract")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--bundle-uid", help="Job Asset Bundle UID")
    selector.add_argument("--job-id", help="Use the latest bundle for this job id")
    parser.add_argument("--validate", action="store_true", help="Validate the manifest contract")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    session_factory: sessionmaker[Session] = SessionLocal,
) -> int:
    args = parse_args(argv)
    with session_factory() as session:
        bundle_uid = args.bundle_uid
        if args.job_id:
            bundle = get_latest_job_asset_bundle_by_job_id(session, args.job_id)
            if bundle is None:
                print("Bundle not found", file=sys.stderr)
                return 1
            bundle_uid = bundle.bundle_uid
        bundle = load_bundle(session, bundle_uid)
        if bundle is None:
            print("Bundle not found", file=sys.stderr)
            return 1
        try:
            manifest = (
                bundle.renderer_manifest_json
                if isinstance(bundle.renderer_manifest_json, dict)
                and bundle.renderer_manifest_json.get("manifest_version") == "1.0"
                else build_renderer_manifest(bundle)
            )
            if args.validate:
                validate_renderer_manifest_contract(manifest)
        except Exception:
            print("Renderer manifest validation failed", file=sys.stderr)
            return 1

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
