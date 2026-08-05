# Legacy Asset Hub retirement

Retirement date: 2026-08-05

The previous Asset Hub infrastructure was retired after the managed Google
Drive pilot was validated.

Removed infrastructure:

- Docker Swarm stack `kurukin-asset-hub`
- Legacy PostgreSQL volume
- Legacy previews volume
- Legacy job-assets volume
- Audit PostgreSQL container and volume
- Physical-clone PostgreSQL container and volume

The legacy database contained physical TOAST corruption and is not a valid
source for the current system.

Cold backup retained at:

`/var/backups/kurukin-asset-hub/legacy-retirement-20260805T051935Z`

The current supported runtime is:

- `kurukin-asset-hub-pilot-db`
- database `kurukin_asset_hub_pilot`
- `PILOT_PREVIEW_ROOT`
- `asset-hub drive ...`
- managed Drive compact_v2 layout

The cold backup is operational data and must not be committed to Git.
