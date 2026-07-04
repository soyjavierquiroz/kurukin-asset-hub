from dataclasses import dataclass

from app.models import Asset, Brand, Product
from app.models.brand_asset_policy import BrandAssetPolicy
from app.models.product_asset_policy import ProductAssetPolicy


@dataclass(frozen=True)
class AssetSearchPolicy:
    allow_global_video: bool = True
    allow_global_image: bool = True
    allow_global_audio: bool = True
    allow_stock_video: bool = True
    allow_stock_image: bool = True
    allow_stock_audio: bool = True
    require_product_match_for_video: bool = False
    require_product_match_for_image: bool = False
    default_usage_scope: str = "brand_exclusive"
    auto_select_enabled_default: bool = True


def resolve_asset_search_policy(
    brand: Brand | None,
    product: Product | None = None,
) -> AssetSearchPolicy:
    policy = AssetSearchPolicy()
    if brand is not None and brand.asset_policy is not None:
        policy = apply_brand_policy(policy, brand.asset_policy)
    if product is not None and product.asset_policy is not None:
        policy = apply_product_policy(policy, product.asset_policy)
    return policy


def apply_brand_policy(
    base: AssetSearchPolicy,
    brand_policy: BrandAssetPolicy,
) -> AssetSearchPolicy:
    allow_global_assets = brand_policy.allow_global_assets
    allow_stock_assets = brand_policy.allow_stock_assets
    return AssetSearchPolicy(
        allow_global_video=brand_policy.allow_global_video and allow_global_assets,
        allow_global_image=brand_policy.allow_global_image and allow_global_assets,
        allow_global_audio=brand_policy.allow_global_audio and allow_global_assets,
        allow_stock_video=brand_policy.allow_stock_video and allow_stock_assets,
        allow_stock_image=brand_policy.allow_stock_image and allow_stock_assets,
        allow_stock_audio=brand_policy.allow_stock_audio and allow_stock_assets,
        require_product_match_for_video=base.require_product_match_for_video,
        require_product_match_for_image=base.require_product_match_for_image,
        default_usage_scope=brand_policy.default_asset_scope,
        auto_select_enabled_default=base.auto_select_enabled_default,
    )


def apply_product_policy(
    base: AssetSearchPolicy,
    product_policy: ProductAssetPolicy,
) -> AssetSearchPolicy:
    if product_policy.inherit_brand_policy:
        return base
    return AssetSearchPolicy(
        allow_global_video=coalesce_bool(product_policy.allow_global_video, base.allow_global_video),
        allow_global_image=coalesce_bool(product_policy.allow_global_image, base.allow_global_image),
        allow_global_audio=coalesce_bool(product_policy.allow_global_audio, base.allow_global_audio),
        allow_stock_video=coalesce_bool(product_policy.allow_stock_video, base.allow_stock_video),
        allow_stock_image=coalesce_bool(product_policy.allow_stock_image, base.allow_stock_image),
        allow_stock_audio=coalesce_bool(product_policy.allow_stock_audio, base.allow_stock_audio),
        require_product_match_for_video=coalesce_bool(
            product_policy.require_product_match_for_video,
            base.require_product_match_for_video,
        ),
        require_product_match_for_image=coalesce_bool(
            product_policy.require_product_match_for_image,
            base.require_product_match_for_image,
        ),
        default_usage_scope=product_policy.default_usage_scope or base.default_usage_scope,
        auto_select_enabled_default=coalesce_bool(
            product_policy.auto_select_enabled_default,
            base.auto_select_enabled_default,
        ),
    )


def is_asset_eligible_for_search(
    asset: Asset,
    brand: Brand | None,
    product: Product | None = None,
    include_global_assets: bool = True,
    include_stock_assets: bool = True,
    policy: AssetSearchPolicy | None = None,
) -> bool:
    if asset.usage_scope == "restricted":
        return False
    if not asset.auto_select_enabled:
        return False
    if asset.rights_status == "restricted":
        return False

    policy = policy or resolve_asset_search_policy(brand=brand, product=product)
    if asset.rights_status == "stock":
        if not include_stock_assets:
            return False
        if not policy_allows_stock(policy, asset.type):
            return False

    if brand is None:
        return include_global_assets and asset.usage_scope == "global"

    if asset.brand_id == brand.id:
        return asset_matches_requested_product(asset, product, policy)

    if asset.usage_scope == "global":
        return include_global_assets and policy_allows_global(policy, asset.type)

    if asset_allowed_for_brand(asset, brand):
        return True

    return False


def asset_matches_requested_product(
    asset: Asset,
    product: Product | None,
    policy: AssetSearchPolicy,
) -> bool:
    if product is None:
        return True
    if asset.product_id == product.id:
        return True
    if asset.type == "video" and policy.require_product_match_for_video:
        return False
    if asset.type == "image" and policy.require_product_match_for_image:
        return False
    return asset.product_id is None


def policy_allows_global(policy: AssetSearchPolicy, asset_type: str) -> bool:
    if asset_type == "video":
        return policy.allow_global_video
    if asset_type == "image":
        return policy.allow_global_image
    if asset_type == "audio":
        return policy.allow_global_audio
    return True


def policy_allows_stock(policy: AssetSearchPolicy, asset_type: str) -> bool:
    if asset_type == "video":
        return policy.allow_stock_video
    if asset_type == "image":
        return policy.allow_stock_image
    if asset_type == "audio":
        return policy.allow_stock_audio
    return True


def asset_allowed_for_brand(asset: Asset, brand: Brand) -> bool:
    return any(allowed.brand_id == brand.id for allowed in asset.allowed_brands)


def coalesce_bool(value: bool | None, fallback: bool) -> bool:
    if value is None:
        return fallback
    return value
