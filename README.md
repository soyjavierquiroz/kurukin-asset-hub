# Kurukin Asset Hub

Bootstrap independiente para el servicio `assets.kuruk.in`.

## Requisitos

- Ubuntu 24.04
- Docker Swarm activo
- Traefik desplegado como servicio Swarm
- Red externa existente: `traefik_public`

## Setup local del stack

Copiar el archivo de ejemplo:

```bash
cp .env.example .env
```

Editar las variables antes de desplegar. Como mínimo, cambiar `POSTGRES_PASSWORD`, `ADMIN_PASSWORD`, `ASSET_HUB_API_KEY` y alinear `DATABASE_URL` con ese valor.

```env
POSTGRES_PASSWORD=una-password-fuerte
DATABASE_URL=postgresql+psycopg://asset_hub:una-password-fuerte@db:5432/kurukin_asset_hub
ADMIN_USERNAME=admin
ADMIN_PASSWORD=otra-password-fuerte
ASSET_HUB_API_KEY=otra-api-key-fuerte
```

## Build y deploy

```bash
make build
set -a; . ./.env; set +a; docker stack deploy -c docker-compose.yml kurukin-asset-hub
```

También se puede usar:

```bash
make deploy
```

## Migraciones

Cuando el stack esté levantado, ejecutar Alembic dentro de la red interna del stack:

```bash
docker run --rm --env-file .env --network kurukin-asset-hub_asset_hub_internal kurukin-asset-hub-web:asset-preview-enrichment alembic upgrade head
```

O usar:

```bash
make migrate
```

Para esta rama, la imagen esperada es:

```bash
docker build -t kurukin-asset-hub-web:ai-asset-enrichment .
```

## Health checks

Probar el endpoint básico:

```bash
curl -I https://assets.kuruk.in/healthz
curl https://assets.kuruk.in/healthz
```

Probar readiness con conexión real a PostgreSQL:

```bash
curl https://assets.kuruk.in/readyz
```

## Catalog data model

El catálogo inicial vive en modelos SQLAlchemy 2.x bajo `app/models/` y se publica con Alembic. Incluye fuentes, perfiles de autenticación, marcas, productos, nichos, colecciones, assets, tags, usos y bundles de trabajo.

`AuthProfile` guarda sólo metadata operativa y referencias como `secret_ref`; no guarda credenciales reales, tokens ni secretos. `Asset` referencia a `Source`, y `Source` puede apuntar a un `AuthProfile`, pero los assets nunca contienen credenciales.

`Asset` también conserva campos de transformación como flip, crop, zoom, speed change, reverse y color grade. Esos flags permiten escoger variaciones visuales sin repetir assets de forma innecesaria ni asumir transformaciones inseguras para una pieza.

## Keywords and AI-ready search

El indexador genera `AssetKeyword` desde filename, source, brand, product y niche. También llena `search_text` y `embedding_text` básico para que renderers automáticos puedan encontrar piezas por texto sin depender todavía de IA ni embeddings reales.

Las keywords ayudan a buscar candidatos; las policies deciden si un asset puede usarse. Un asset con keyword perfecta no debe ser seleccionado si su policy lo vuelve inelegible para la marca, producto, tipo o rights status de la búsqueda.

`Asset` expone:

- `usage_scope`: `global`, `brand_exclusive`, `allowed_brands`, `collection_only`, `restricted`.
- `rights_status`: `owned`, `licensed`, `stock`, `unknown`, `restricted`.
- `auto_select_enabled`: apaga cualquier selección automática sin borrar el asset.
- `negative_keywords`: términos a evitar en futuras mejoras de matching.

## Brand/Product inherited asset policies

La elegibilidad se hereda desde marca y producto:

- Si existe `ProductAssetPolicy` con `inherit_brand_policy=false`, sus valores no nulos sobrescriben la policy de marca.
- Si `inherit_brand_policy=true` o no existe policy de producto, se usa `BrandAssetPolicy`.
- Si no existe `BrandAssetPolicy`, se usan defaults del sistema.

Las policies controlan si se permiten assets globales o stock por tipo (`video`, `image`, `audio`) y si un producto requiere match estricto para video/image. `AssetAllowedBrand` se mantiene como override excepcional para permitir un asset específico en otra marca; no es el flujo principal de permisos.

## Renderer search API

`GET /api/assets/search` está pensado para renderers y requiere API key en header. `/healthz` y `/readyz` siguen públicos.

```bash
curl "https://assets.kuruk.in/api/assets/search?brand_slug=brand_a&type=video" \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}"
```

Parámetros principales:

- `q`, `brand_slug`, `product_slug`, `niche_slug`
- `type`, `orientation`, `usage_scope`
- `include_global_assets=true`
- `include_stock_assets=true`
- `limit`

Reglas clave: `restricted` nunca aparece, `auto_select_enabled=false` nunca aparece, `brand_exclusive` de otra marca no aparece, y assets globales sólo aparecen si el request y la policy efectiva lo permiten.

## Asset selection API

`GET /api/assets/search` es exploratorio: permite inspeccionar candidatos por texto, marca, producto, tipo y orientación. `POST /api/assets/select` está pensado para render jobs: recibe contexto de escena, respeta las mismas policies de marca/producto, rankea por utilidad creativa y devuelve una lista diversificada para que un worker futuro pueda copiar, descargar o renderizar.

`POST /api/assets/select` requiere `X-Asset-Hub-Api-Key`. `brand_slug` es obligatorio; si una marca o producto no existe, responde `200` con `count=0` y `assets=[]`. La API no copia archivos todavía y no publica media sin autenticación: `thumbnail_url` y `preview_url` son informativos y apuntan a rutas internas bajo `/media/previews/...`, protegidas por Basic Auth en la UI.

Ejemplo para Grandiosa Mujer / Veyra:

```bash
curl -sS \
  -X POST \
  -H "Content-Type: application/json" \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  https://assets.kuruk.in/api/assets/select \
  -d '{
    "brand_slug": "grandiosa_mujer",
    "product_slug": "veyra",
    "script_scene": "Veyra revela energía mística mientras mira el celular",
    "asset_type": "video",
    "orientation": "9:16",
    "count": 3,
    "require_preview_ready": true,
    "allow_needs_review": true
  }'
```

La respuesta incluye `rclone_remote` y `remote_path` para que un worker futuro sepa dónde está el master, junto con metadata técnica/editorial, flags de transformación permitida, `score` y `match_reasons`. `include_restricted=true` se rechaza en este MVP; assets `restricted`, `rights_status=restricted` o `auto_select_enabled=false` nunca son seleccionados.

## Job Asset Bundles

`POST /api/assets/select` selecciona assets para una escena individual. `POST /api/jobs/asset-bundles` crea un manifest persistente para un job completo: guarda cada escena, los assets seleccionados, `score`, `match_reasons` y la metadata necesaria para que un worker futuro reproduzca la selección.

La API de bundles no copia masters todavía. Devuelve y persiste `rclone_remote` + `remote_path` para que un renderer futuro pueda resolver los archivos desde el remote autorizado. Si `force=false`, se reutiliza el último bundle `ready` del mismo `job_id`; si `force=true`, se genera un bundle nuevo y los anteriores del job quedan `superseded`.

Ejemplo para Grandiosa Mujer / Veyra:

```bash
curl -sS \
  -X POST \
  -H "Content-Type: application/json" \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  https://assets.kuruk.in/api/jobs/asset-bundles \
  -d '{
    "job_id": "mpt-test-veyra-001",
    "brand_slug": "grandiosa_mujer",
    "product_slug": "veyra",
    "force": true,
    "created_by": "manual-test",
    "scenes": [
      {
        "scene_id": "scene-001",
        "scene_index": 1,
        "script_scene": "Veyra revela energía mística mientras mira el celular",
        "asset_type": "video",
        "orientation": "9:16",
        "count": 2,
        "require_preview_ready": true,
        "allow_needs_review": true
      },
      {
        "scene_id": "scene-002",
        "scene_index": 2,
        "script_scene": "Veyra habla por teléfono en un ambiente místico",
        "asset_type": "video",
        "orientation": "9:16",
        "count": 2,
        "require_preview_ready": true,
        "allow_needs_review": false
      }
    ]
  }'
```

Consultar un bundle:

```bash
curl -sS \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  https://assets.kuruk.in/api/jobs/mpt-test-veyra-001/asset-bundle
```

## Job Bundle Materialization

Un Job Asset Bundle selecciona assets y conserva `rclone_remote` + `remote_path`.
La materialización copia sólo esos masters seleccionados al volumen local persistente; no copia
toda la biblioteca y no publica archivos materializados como rutas públicas.

La configuración de rclone queda fuera del repo. `rclone.conf` no se commitea ni se monta en el
servicio web por defecto. La API de materialización sólo funciona si `RCLONE_CONFIG` existe dentro
del contenedor; si falta, responde con un error claro sin tumbar la app. El método recomendado por
ahora es la CLI con `rclone.conf` montado read-only.

Volumen persistente:

```text
kurukin-asset-hub_job_assets:/data/job-assets
```

CLI recomendado:

```bash
docker run --rm \
  --env-file .env \
  --network kurukin-asset-hub_asset_hub_internal \
  -v /root/.config/rclone/rclone.conf:/config/rclone/rclone.conf:ro \
  -e RCLONE_CONFIG=/config/rclone/rclone.conf \
  -v kurukin-asset-hub_job_assets:/data/job-assets \
  kurukin-asset-hub-web:job-bundle-materialization \
  python scripts/materialize_job_bundle.py --bundle-uid jab_xxx --force
```

Obtener el renderer manifest:

```bash
curl -sS \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  https://assets.kuruk.in/api/jobs/asset-bundles/jab_xxx/renderer-manifest
```

## Renderer Manifest Contract

Endpoint principal:

```text
GET /api/jobs/asset-bundles/{bundle_uid}/renderer-manifest
```

El contrato estable actual usa `manifest_version: "1.0"` y está pensado para que un
renderer externo consuma assets materializados sin leer la DB ni conocer la lógica interna
de Asset Hub.

Un renderer debe leer `scenes` en orden y, dentro de cada escena, consumir `assets` por
`rank`. Cada asset incluye `local_path`, `relative_path`, metadatos creativos, permisos de
transformación, `recommended_transform` y `render_warnings`. `local_path` es un path dentro
del contenedor/volumen, no una URL pública.

Para un renderer en otro contenedor, montar el mismo volumen en modo read-only:

```text
kurukin-asset-hub_job_assets:/data/job-assets:ro
```

El renderer no debe leer PostgreSQL. Tampoco necesita rclone cuando el bundle ya fue
materializado: debe usar `local_path` o combinar `storage.storage_dir` con `relative_path`.
`recommended_transform` resume decisiones seguras como `crop_mode`, `scale_mode`,
`allow_zoom`, `allow_speed_change` y `subtitle_safe_area`. `render_warnings` permite
degradar o rechazar assets que requieran revisión, tengan texto visible, watermark o archivo
materializado faltante.

Ejemplo curl:

```bash
curl -sS \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  https://assets.kuruk.in/api/jobs/asset-bundles/jab_xxx/renderer-manifest
```

Schema JSON del contrato:

```bash
curl -sS \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  https://assets.kuruk.in/api/renderer-manifest/schema
```

Ejemplo de contenedor renderer con volumen read-only:

```bash
docker run --rm \
  -v kurukin-asset-hub_job_assets:/data/job-assets:ro \
  renderer-image:latest \
  renderer --manifest /data/job-assets/jab_xxx/manifests/renderer-manifest.json
```

## Admin UI

La UI interna vive en `/` y está renderizada con FastAPI, Jinja2, HTMX y Tailwind CDN. Permite revisar conteos del catálogo, filtrar assets, ver detalle técnico/editorial/transformación/seguridad y administrar sources, brands, products y niches con formularios server-rendered.

Las rutas web admin usan Basic Auth simple. Configurar estas variables en `.env` antes de publicar:

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=otra-password-fuerte
ASSET_HUB_API_KEY=otra-api-key-fuerte
```

`/healthz` y `/readyz` permanecen públicos para health checks y readiness externos.

## Indexing assets with rclone

El indexador MVP lee metadata con `rclone lsjson` y crea o actualiza assets en PostgreSQL sin descargar archivos. No guarda credenciales de rclone en `sources` ni en `assets`; la configuración queda fuera del servicio y se monta sólo al ejecutar el script.

Requisito: tener un `rclone.conf` válido en el host y confirmar el nombre del remote:

```bash
rclone listremotes
```

Ejemplo local:

```bash
python scripts/index_rclone_source.py \
  --source-id drive_mujer_no_escribas \
  --remote gdrive_mne \
  --root "Assets Mujer No Escribas" \
  --brand mujer_no_escribas \
  --product metodo_pausa \
  --default-niche relaciones
```

Ejemplo Docker con `rclone.conf` montado read-only:

```bash
docker run --rm \
  --env-file .env \
  --network kurukin-asset-hub_asset_hub_internal \
  -v /root/.config/rclone/rclone.conf:/config/rclone/rclone.conf:ro \
  -e RCLONE_CONFIG=/config/rclone/rclone.conf \
  kurukin-asset-hub-web:asset-preview-enrichment \
  python scripts/index_rclone_source.py \
    --source-id drive_mujer_no_escribas \
    --remote gdrive_mne \
    --root "Assets Mujer No Escribas" \
    --brand mujer_no_escribas \
    --product metodo_pausa \
    --default-niche relaciones
```

Los assets se deduplican por `source + remote_path`; si un archivo ya existe para esa fuente, el indexador actualiza metadata como filename, extensión, tamaño, remote y fecha de indexado. El servicio web no monta `rclone.conf` por defecto y no falla si ese archivo no existe.

## Previews and technical enrichment

Los archivos maestros no se guardan en el servidor. Para enriquecer un asset, el servicio descarga una copia temporal desde el remote rclone configurado en el asset, ejecuta `ffprobe`/`ffmpeg`, borra el temporal al terminar y conserva sólo previews livianos en un volumen Docker persistente.

El volumen de previews se monta en:

```env
PREVIEW_STORAGE_DIR=/data/previews
```

En Swarm, el volumen usado por el servicio web es `kurukin-asset-hub_previews` y se monta en `/data/previews`. Los paths guardados en DB son relativos, por ejemplo `previews/<asset_uid>/thumbnail.jpg` o `previews/<asset_uid>/preview.mp4`.

`rclone.conf` no se commitea y no se monta en el servicio web por defecto. Sólo debe montarse en scripts o acciones que necesitan descargar temporalmente desde el remote.

Generar o regenerar un asset desde la UI:

- Entrar a `/assets/{id}` con Basic Auth.
- Usar `Generate Preview`.
- Si ya existe preview, el botón muestra `Regenerate Preview`.

También se puede seleccionar assets en `/assets` y ejecutar `Generate previews for selected`.

Ejemplo CLI por asset ID:

```bash
docker run --rm \
  --env-file .env \
  --network kurukin-asset-hub_asset_hub_internal \
  -v /root/.config/rclone/rclone.conf:/config/rclone/rclone.conf:ro \
  -e RCLONE_CONFIG=/config/rclone/rclone.conf \
  -v kurukin-asset-hub_previews:/data/previews \
  kurukin-asset-hub-web:asset-preview-enrichment \
  python scripts/enrich_asset_previews.py --asset-id 1
```

Ejemplo CLI para pendientes:

```bash
docker run --rm \
  --env-file .env \
  --network kurukin-asset-hub_asset_hub_internal \
  -v /root/.config/rclone/rclone.conf:/config/rclone/rclone.conf:ro \
  -e RCLONE_CONFIG=/config/rclone/rclone.conf \
  -v kurukin-asset-hub_previews:/data/previews \
  kurukin-asset-hub-web:asset-preview-enrichment \
  python scripts/enrich_asset_previews.py --pending --limit 10
```

Agregar `--force` para regenerar previews existentes.

## AI asset enrichment

El enriquecimiento visual con IA usa thumbnails, previews de imagen y frames sampleados desde `preview.mp4`. No guarda masters, no guarda frames temporales y no decide permisos de marca/producto: las keywords y descripciones ayudan a buscar, pero `usage_scope`, rights status y las policies de marca/producto siguen mandando sobre elegibilidad.

Configurar:

```env
OPENAI_API_KEY=
AI_ENRICHMENT_ENABLED=false
AI_PROVIDER=openai
AI_MODEL=
AI_FRAME_SAMPLE_COUNT=8
AI_REVIEW_THRESHOLD=0.72
AI_MAX_ASSETS_PER_BATCH=20
```

`OPENAI_API_KEY` es opcional para levantar la app web. Para llamadas reales, definir `OPENAI_API_KEY`, poner `AI_ENRICHMENT_ENABLED=true` y, opcionalmente, fijar `AI_MODEL`. Con `AI_ENRICHMENT_ENABLED=false`, los assets se marcan como `skipped` con razón clara, salvo `--dry-run`, que no llama al proveedor ni muta el asset.

El resultado completo validado se guarda en `AssetAIAnalysis`. El servicio actualiza metadata creativa, flags de seguridad/transformación, `search_text`, `embedding_text`, `best_for`, `avoid_for` y keywords `source=ai`. No borra ni sobrescribe keywords manuales; con `--force` reemplaza sólo keywords previas `source=ai`.

Ejemplo por asset:

```bash
docker run --rm \
  --env-file .env \
  --network kurukin-asset-hub_asset_hub_internal \
  -v kurukin-asset-hub_previews:/data/previews \
  kurukin-asset-hub-web:ai-asset-enrichment \
  python scripts/enrich_assets_ai.py --asset-id 1
```

Ejemplo batch pending:

```bash
docker run --rm \
  --env-file .env \
  --network kurukin-asset-hub_asset_hub_internal \
  -v kurukin-asset-hub_previews:/data/previews \
  kurukin-asset-hub-web:ai-asset-enrichment \
  python scripts/enrich_assets_ai.py --pending --limit 10
```

Dry-run:

```bash
docker run --rm \
  --env-file .env \
  --network kurukin-asset-hub_asset_hub_internal \
  -v kurukin-asset-hub_previews:/data/previews \
  kurukin-asset-hub-web:ai-asset-enrichment \
  python scripts/enrich_assets_ai.py --pending --limit 10 --dry-run
```

Revisar assets que requieren atención:

- En la UI, filtrar `/assets` por `AI status=needs_review` o `Needs review=Yes`.
- En detalle de asset, la sección `AI Enrichment` muestra status, confidence, modelo, razón de revisión y errores sanitizados.

## Revisión de Traefik y servicios

```bash
docker service ls | grep kurukin-asset-hub
docker service logs kurukin-asset-hub_web --tail 100
docker service inspect traefik_traefik --format '{{json .Spec.TaskTemplate.Networks}}'
docker network inspect traefik_public
```

Las labels de Traefik están en `deploy.labels`, como requiere Docker Swarm:

```yaml
traefik.enable: "true"
traefik.docker.network: traefik_public
traefik.http.routers.asset-hub.rule: Host(`assets.kuruk.in`)
traefik.http.routers.asset-hub.entrypoints: websecure
traefik.http.routers.asset-hub.tls.certresolver: le
traefik.http.services.asset-hub.loadbalancer.server.port: "8000"
```

## Tests

```bash
make test
```

## Source Sync UI

Source Sync UI permite sincronizar assets desde una `Source` existente respaldada por rclone. No es un uploader: los masters siguen viviendo en Drive/rclone y el hub sólo registra o actualiza metadata local.

Flujo:

1. Abrir `/sources`.
2. Entrar a una source.
3. Ejecutar `Scan source` para guardar un dry-run.
4. Revisar el reporte por `new`, `existing`, `moved`, `changed`, `missing`, `unsupported` y `errors`.
5. Ejecutar `Apply sync` para crear/actualizar assets.

Reglas operativas:

- El remote rclone debe existir en el servidor.
- `missing` marca assets como faltantes, no los borra.
- Un re-scan es incremental.
- `moved` actualiza `remote_path`.
- `changed` manda preview, metadata técnica e IA a `pending`.
- Sync no borra previews, IA ni keywords manuales.

CLI:

```bash
python scripts/sync_rclone_source.py --source-id drive_grandiosa_mujer_veyra --dry-run
python scripts/sync_rclone_source.py --source-id drive_grandiosa_mujer_veyra --apply
```
