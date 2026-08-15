from __future__ import annotations

from dataclasses import dataclass

from app.models import Asset


class AssetLocationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssetRcloneLocation:
    remote: str
    remote_path: str


def resolve_asset_rclone_location(asset: Asset) -> AssetRcloneLocation:
    remote = clean_path_part(asset.rclone_remote) or (
        clean_path_part(asset.source.rclone_remote) if asset.source else None
    )
    logical_path = clean_remote_path(asset.remote_path) or clean_remote_path(asset.source_path)
    if not remote:
        raise AssetLocationError("asset rclone remote is missing")
    if not logical_path:
        raise AssetLocationError("asset remote path is missing")
    return AssetRcloneLocation(
        remote=remote,
        remote_path=resolve_rclone_source_path(
            logical_path,
            asset.source.root_path if asset.source else None,
        ),
    )


def resolve_rclone_source_path(remote_path: str, root_path: str | None) -> str:
    clean_remote_path_value = clean_remote_path(remote_path)
    if not clean_remote_path_value:
        raise AssetLocationError("asset remote path is missing")
    clean_root_path = clean_remote_path(root_path)
    if not clean_root_path:
        return clean_remote_path_value
    if (
        clean_remote_path_value == clean_root_path
        or clean_remote_path_value.startswith(f"{clean_root_path}/")
    ):
        return clean_remote_path_value
    return f"{clean_root_path}/{clean_remote_path_value}"


def clean_remote_path(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip("/")
    return cleaned or None


def clean_path_part(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None
