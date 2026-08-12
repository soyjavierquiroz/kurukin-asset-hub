# Asset Kurukin Media / MoneyPrinterTurbo contract

Fecha de actualizacion: 2026-08-12.

Fuente de verdad usada: repositorio local en `/opt/apps/kurukin-asset-hub`, Docker local y base PostgreSQL pilot local.

Este documento refleja el contrato implementado para MPT:

- MPT busca assets con `POST /api/assets/search`.
- El identificador publico/canonico es `asset_uid`.
- `asset_id` se conserva por compatibilidad y tiene el mismo valor que `asset_uid`.
- MPT crea Job Asset Bundles con `POST /api/jobs/asset-bundles`.
- Para seleccion positiva, MPT envia `selected_asset_uids` por escena.
- La materializacion existe por Job Asset Bundle, no por asset individual.
- El servicio pilot observado todavia tiene la materializacion deshabilitada.

## Conectividad

### EXISTE HOY

- App FastAPI: `Kurukin Asset Hub`, version `0.1.0`.
- Base path API: `/api`.
- Search MPT: `POST /api/assets/search`.
- Health publico: `GET /healthz`, `HEAD /healthz`.
- Readiness publico con DB: `GET /readyz`.
- OpenAPI generado por FastAPI:
  - `GET /openapi.json`
  - UI Swagger default: `GET /docs`
  - UI ReDoc default: `GET /redoc`
- Puerto interno del contenedor: `8000`.
- Dockerfile expone `8000` y corre `uvicorn app.main:app --host 0.0.0.0 --port 8000`.

Configuracion declarada en `docker-compose.yml`:

- Servicio Docker: `web`.
- Stack esperado: `kurukin-asset-hub`.
- Nombre Swarm esperado: `kurukin-asset-hub_web`.
- Imagen default: `kurukin-asset-hub-web:job-bundle-materialization`.
- Redes:
  - `kurukin-asset-hub_asset_hub_internal`
  - `traefik_public`
- Host Traefik declarado: `https://assets.kuruk.in`.
- Servicio Traefik apunta al puerto interno `8000`.
- Volumen de materializacion declarado: `job_assets:/data/job-assets`.

Estado Docker observado en este servidor:

- Servicio corriendo: `kurukin-asset-hub-pilot_web`.
- Contenedor/tarea observado: `kurukin-asset-hub-pilot_web.1...`.
- Imagen observada: `kurukin-asset-hub-web:pilot-ux-5fc0605`.
- Puerto interno observado: `8000/tcp`.
- No hay puerto publicado directo en el servicio web pilot; el acceso debe ir por red Docker/Traefik o desde otro contenedor en la misma red.
- DB pilot observada: `kurukin-asset-hub-pilot-db`, con Postgres en host `127.0.0.1:55491->5432/tcp`.
- Redes observadas relevantes:
  - `kurukin_asset_hub_pilot_internal`
  - `kurukin-asset-hub_default`
  - `traefik_public`

Desde MoneyPrinterTurbo en el mismo servidor, el consumo recomendado hoy depende de donde corre MPT:

- Si MPT corre en Docker: HTTP por red Docker compartida hacia el servicio web, puerto `8000`. En el stack principal seria `http://web:8000` desde la misma red del stack, o el nombre DNS real del servicio si MPT se adjunta a la red Swarm correspondiente.
- Si MPT corre fuera de Docker: HTTP via Traefik usando `https://assets.kuruk.in`, o publicar un puerto local controlado. No hay puerto web local publicado en el servicio pilot observado.
- Para materializacion por filesystem, MPT tendria que compartir el volumen de assets materializados. En el stack principal el volumen declarado es `kurukin-asset-hub_job_assets:/data/job-assets`. En el servicio pilot observado la materializacion esta deshabilitada y no hay volumen `job_assets` montado.

Variables de entorno necesarias o relevantes, solo nombres:

- Servicio/API:
  - `ASSET_HUB_HOST`
  - `ASSET_HUB_API_KEY`
  - `DATABASE_URL`
  - `APP_ENV`
  - `LOG_LEVEL`
- DB:
  - `POSTGRES_DB`
  - `POSTGRES_USER`
  - `POSTGRES_PASSWORD`
- Admin UI:
  - `ADMIN_USERNAME`
  - `ADMIN_PASSWORD`
- Materializacion:
  - `JOB_ASSETS_STORAGE_DIR`
  - `JOB_ASSET_MATERIALIZATION_ENABLED`
  - `RCLONE_CONFIG`
  - `RCLONE_REMOTE`
- Storage/Drive/rclone:
  - `GOOGLE_DRIVE_CLIENT`
  - `GOOGLE_DRIVE_AUTH_MODE`
  - `GOOGLE_APPLICATION_CREDENTIALS`
  - `GOOGLE_DRIVE_ROOT_FOLDER_ID`
  - `GOOGLE_DRIVE_GENERIC_INBOX_FOLDER_ID`
  - `GOOGLE_DRIVE_BRAND_INBOX_FOLDER_ID`
  - `GOOGLE_DRIVE_TITLE_INBOX_FOLDER_ID`
  - `GOOGLE_DRIVE_GENERIC_LIBRARY_FOLDER_ID`
  - `GOOGLE_DRIVE_BRAND_LIBRARY_FOLDER_ID`
  - `GOOGLE_DRIVE_TITLE_LIBRARY_FOLDER_ID`
  - `GOOGLE_DRIVE_REVIEW_FOLDER_ID`
  - `GOOGLE_DRIVE_ERROR_FOLDER_ID`
- Previews:
  - `PREVIEW_STORAGE_DIR`
  - `PILOT_PREVIEW_ROOT`
- IA/enrichment no requerido para MPT search/materializacion:
  - `AI_ENRICHMENT_ENABLED`
  - `AI_PROVIDER`
  - `AI_MODEL`
  - `AI_OUTPUT_LANGUAGE`
  - `AI_FRAME_SAMPLE_COUNT`
  - `AI_REVIEW_THRESHOLD`
  - `AI_MAX_ASSETS_PER_BATCH`
  - `OPENAI_API_KEY`
  - `NVIDIA_API_KEY`
  - `NVIDIA_BASE_URL`
  - `NVIDIA_MODEL`
  - `NVIDIA_MAX_TOKENS`
  - `NVIDIA_MAX_CONCURRENCY`

### NO EXISTE

- No existe un puerto HTTP local publicado para el servicio web pilot observado.
- No existe contrato documentado en repo para un nombre DNS Docker estable de MPT hacia Asset Hub.

### RECOMENDACION

- Para MPT en Docker, conectar MPT a la red interna donde viva Asset Hub y usar HTTP interno.
- Para materializacion sin copias innecesarias, montar un volumen comun read-only en MPT despues de materializar:

```text
kurukin-asset-hub_job_assets:/data/job-assets:ro
```

## Autenticacion

### EXISTE HOY

Tipo: API key estatica por header.

Header exacto:

```text
X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}
```

Variable donde debe vivir la credencial:

```text
ASSET_HUB_API_KEY
```

Respuestas:

- Header ausente: HTTP `401`, body:

```json
{"detail": "API key required"}
```

- Header presente pero invalido: HTTP `403`, body:

```json
{"detail": "Invalid API key"}
```

### NO EXISTE

- No hay OAuth, JWT, mTLS ni Basic Auth para endpoints API MPT.
- La Basic Auth existe para UI admin, no para `/api/assets/search`.

## Search API

Endpoint:

```text
POST /api/assets/search
```

### Request JSON real

Schema real aceptado por Pydantic:

```json
{
  "query": "string | null",
  "limit": 20,
  "source_policy": {
    "sources": [
      {
        "scope": "generic | brand | title",
        "brand": "string | null",
        "title": "string | null"
      }
    ]
  }
}
```

Campos:

- `query`: opcional, `string | null`. Si viene vacio o solo espacios, no aplica filtro textual. Si contiene texto, se tokeniza y cada token debe matchear algun campo textual/metadata.
- `limit`: opcional, entero `1..200`, default `20`. No existe `offset`, `page`, cursor ni paginacion real.
- `source_policy`: opcional. Si falta, se usa:

```json
{
  "sources": [
    {"scope": "generic", "brand": null, "title": null}
  ]
}
```

- `source_policy.sources`: lista obligatoria si `source_policy` existe, minimo 1 item.
- `sources[].scope`: obligatorio. Valores validos:
  - `generic`
  - `brand`
  - `title`
- `sources[].brand`: requerido y no vacio cuando `scope="brand"`. Es slug de marca.
- `sources[].title`: requerido y no vacio cuando `scope="title"`. Es slug de title/serie.

Filtros disponibles en `POST /api/assets/search`:

- Scope/source por `source_policy.sources`.
- Texto por `query`.
- Limite por `limit`.

Filtros que NO existen en `POST /api/assets/search`:

- `media_type` o `type`.
- `orientation`.
- Aspect ratio separado.
- Duracion minima/maxima.
- Resolucion minima.
- `width`, `height`, `min_width`, `min_height`.
- `offset`, `page`, `cursor`.
- Orden/ranking configurable.
- `brand` o `title` como campos top-level.
- `scope` top-level.

Nota: `GET /api/assets/search` si tiene filtros como `type`, `orientation`, `brand_slug`, `product_slug`, `niche_slug`, `scope`, `brand`, `title`, `limit`, etc. Ese NO es el contrato MPT solicitado para `POST /api/assets/search`.

### Semantica de sources

`generic`:

- Filtro SQL real: `Asset.scope == "generic"`.
- No mezcla brand/title salvo que se agreguen explicitamente otras sources.

`brand:<slug>`:

- Representacion real en request:

```json
{"scope": "brand", "brand": "grandiosa-mujer"}
```

- Filtro SQL real: `Asset.scope == "brand"` y `Asset.brand.slug == <slug>`.
- No incluye generic automaticamente.
- No incluye otras marcas.

`title:<slug>`:

- Representacion real en request:

```json
{"scope": "title", "title": "mi-otra-yo"}
```

- Filtro SQL real: `Asset.scope == "title"` y `Asset.title_slug == <slug>`.
- No incluye generic automaticamente.
- No incluye otros titles.

Multiples sources en una misma busqueda:

- EXISTE HOY: son OR, no cuotas.
- Implementacion real: se construye una lista de filtros y se devuelve `or_(*filters)`.
- Ejemplo `generic + title` busca assets que sean generic OR title `mi-otra-yo`.
- No hay cuota por source, no hay balanceo por source, no hay garantia de al menos N generic o N title. El ranking global decide el orden final.

### Elegibilidad y ranking real

Antes de rankear, `POST /api/assets/search` exige:

- `Asset.status in ("ready", "moved")`
- `Asset.move_status == "moved"`
- `Asset.source_status` no esta en `("missing", "inaccessible", "deleted")`
- `Asset.usage_scope != "restricted"`
- `Asset.auto_select_enabled is True`
- `Asset.rights_status != "restricted"`
- source policy matchea por OR
- si `query` tiene tokens, todos deben pasar filtro textual SQL y luego score positivo

Orden inicial de candidatos:

1. `quality_score` descendente, null como 0.
2. `ai_enrichment_confidence` descendente, null como 0.
3. `usage_count` ascendente.
4. `id` descendente.

Luego se calcula `score_asset_for_query(asset, tokens)`. Orden final:

1. score descendente.
2. `quality_score` descendente.
3. `ai_enrichment_confidence` descendente.
4. `id` descendente.

La respuesta `POST /api/assets/search` no expone el score, aunque se usa internamente.

## Search response

### Schema real de `POST /api/assets/search`

```json
{
  "query": "string | null",
  "source_policy": {
    "sources": [
      {
        "scope": "generic | brand | title",
        "brand": "string | null",
        "title": "string | null"
      }
    ]
  },
  "count": 0,
  "assets": [
    {
      "asset_id": "string",
      "asset_uid": "string",
      "drive_file_id": "string | null",
      "scope": "generic | brand | title | null",
      "brand": "string | null",
      "collection": "string | null",
      "title_type": "movie | series | null",
      "title": "string | null",
      "title_context": "string | null",
      "filename": "string",
      "target_path": "string",
      "media_type": "video | image | audio | unknown",
      "orientation": "string",
      "primary_theme": "string | null",
      "primary_topic": "string | null",
      "tags": ["string"]
    }
  ]
}
```

Campos garantizados por schema/DB en cada asset de esta respuesta:

- `asset_id`: existe siempre en respuesta. Es `Asset.asset_uid`, string, no el integer PK `Asset.id`. Se conserva por compatibilidad.
- `asset_uid`: existe siempre en respuesta. Es el identificador publico/canonico para MPT.
- `filename`: existe siempre.
- `target_path`: existe siempre y corresponde a `Asset.remote_path`.
- `media_type`: existe siempre y corresponde a `Asset.type`.
- `orientation`: existe siempre, default posible `unknown`.
- `tags`: existe siempre como lista, puede venir vacia.

Campos presentes pero no garantizados no-null:

- `drive_file_id`
- `scope`
- `brand`
- `collection`
- `title_type`
- `title`
- `title_context`
- `primary_theme`
- `primary_topic`

Campos solicitados por MPT:

| Campo | Existe en `POST /api/assets/search` | Observacion |
| --- | --- | --- |
| `asset_id` | Si | Compatibilidad. Mismo valor que `asset_uid`. No es integer. |
| `asset_uid` | Si | Identificador publico/canonico para MPT. |
| `drive_file_id` | Si | Puede ser null. |
| `filename` | Si | Garantizado. |
| `media_type` | Si | Viene de `Asset.type`. |
| `mime_type` | No | Existe en DB, pero no se serializa en esta respuesta. |
| `width` | No | Existe en DB, pero no se serializa en esta respuesta. |
| `height` | No | Existe en DB, pero no se serializa en esta respuesta. |
| `duration` | No | Existe en DB como `duration_seconds`, pero no se serializa en esta respuesta. |
| `size_bytes` | No | Existe en DB, pero no se serializa en esta respuesta. |
| `scope` | Si | Puede ser null a nivel modelo; para assets elegibles MPT normalmente sera generic/brand/title. |
| `brand_slug` | No con ese nombre | La respuesta usa `brand`. Solo se llena para `scope="brand"`. |
| `title_slug` | No | La respuesta usa `title` como nombre humano y `title_context`; no devuelve slug. |
| `tags` | Si | Lista de strings. |
| `keywords` | No | Existen en DB, pero no se serializan en esta respuesta. |
| relevancia/`score` | No | Se calcula internamente, pero no se devuelve. |
| `thumbnail`/`preview` | No | Existen rutas de preview en otros endpoints/responses, no aqui. |
| `checksum`/`hash` | No | Existen `checksum` y `source_hash` en DB, no se serializan aqui. |

### NO EXISTE

- No existe en `POST /api/assets/search` un schema completo para render con metadata tecnica.
- No existe `download_url`, `local_path` ni `preview_url` en esta respuesta.

### RECOMENDACION

- Si MPT necesita metadata tecnica en search, extender `serialize_money_printer_asset` con campos existentes en DB: `mime_type`, `width`, `height`, `duration_seconds`, `size_bytes`, `checksum`, `source_hash`, `preview_url`, `thumbnail_url`, `score`, `brand_slug`, `title_slug`.
- Mantener backwards compatibility agregando campos, no renombrando los actuales.

## Identidad estable

### EXISTE HOY

Hay dos identificadores:

- `Asset.id`: integer PK interno de DB.
- `Asset.asset_uid`: string unico, serializado como `asset_uid` y tambien como `asset_id` en `POST /api/assets/search`.

En la base pilot observada, ejemplos de `asset_uid` son `drive-52cee0a6`, `drive-f78b5e8f`, etc.

El campo `asset_uid` tiene constraint `unique=True` y es obligatorio. En la respuesta MPT, `asset_uid` es el campo canonico y `asset_id` mantiene el mismo valor por compatibilidad.

El modelo tambien tiene constraint unico por:

```text
source_id + remote_path
```

Esto deduplica el mismo path dentro de la misma source.

### Confirmacion solicitada

Identificador recomendado para MPT hoy:

```text
kurukin_media:<asset_uid>
```

donde `<asset_uid>` es el string recibido en la respuesta `POST /api/assets/search`, por ejemplo:

```text
kurukin_media:drive-52cee0a6
```

### Limites de garantia

- `asset_uid` es unico en DB y obligatorio.
- El codigo no contiene un mecanismo que cambie `asset_uid` durante search/materializacion.
- No hay comentario/migracion que declare formalmente `asset_uid` como permanente e inmutable para siempre.
- `Asset.id` puede ser estable dentro de una DB concreta, pero no es el identificador que devuelve `POST /api/assets/search`.

Sobre si el mismo archivo puede tener mas de un `asset_id`:

- Dentro de la misma `source_id + remote_path`, NO deberia: hay unique constraint.
- El mismo archivo fisico podria tener mas de un asset si aparece en otra source, otro remote/path, como derivado, o si se reingesta bajo otro path/source. No hay unique global por checksum/hash.

### RECOMENDACION

- Para dedupe entre jobs, MPT debe almacenar `kurukin_media:<asset_uid>` usando el campo `asset_uid` de `POST /api/assets/search`.
- Asset Hub deberia declarar `asset_uid` como contrato permanente/inmutable y no reutilizable.
- Para dedupe de mismo binario entre sources, agregar contrato opcional por `checksum`/`source_hash` cuando se exponga.

## MATERIALIZACION

### EXISTE HOY

No existe endpoint de materializacion individual por asset resultado de `POST /api/assets/search`.

Si existe materializacion por Job Asset Bundle:

- Crear bundle:

```text
POST /api/jobs/asset-bundles
```

Request de creacion de bundle para MPT:

```json
{
  "job_id": "mpt-001",
  "brand_slug": "grandiosa-mujer",
  "product_slug": null,
  "niche_slug": null,
  "created_by": "money-printer-turbo",
  "scenes": [
    {
      "scene_id": "scene-001",
      "scene_index": 1,
      "script_scene": "Escena uno",
      "selected_asset_uids": ["drive-52cee0a6"]
    }
  ],
  "global_exclude_asset_uids": []
}
```

`selected_asset_uids`:

- Campo opcional por escena.
- Si esta ausente o vacio, esa escena usa auto-selection existente mediante `select_assets(...)`.
- Si trae valores, esa escena NO usa ranking/search/auto-selection.
- Cada UID se resuelve exactamente por `Asset.asset_uid`.
- El orden recibido define `rank`: primer UID `rank=1`, segundo UID `rank=2`.
- `score` queda `null` para seleccion explicita.
- `match_reasons` usa `["explicit_selection"]`.
- No se agregan assets extra para completar `count`.

`brand_slug`:

- Es obligatorio si alguna escena usa auto-selection.
- Puede omitirse o ser `null` solo si TODAS las escenas tienen `selected_asset_uids` no vacio.
- En bundles explicit-only, los assets pueden ser `generic`, `brand` o `title` sin obligar a MPT a enviar una marca ficticia.

Validaciones de seleccion explicita:

- UID inexistente: `422`.
- Asset no elegible: `422`.
- UID seleccionado y excluido por `exclude_asset_uids` o `global_exclude_asset_uids`: `422`.
- UID duplicado dentro de una misma escena: `422`.
- Mismo UID en escenas distintas: permitido.

Elegibilidad para assets explicitos:

- `status in ("ready", "moved")`
- `move_status == "moved"`
- `source_status not in ("missing", "inaccessible", "deleted")`
- `usage_scope != "restricted"`
- `rights_status != "restricted"`
- `auto_select_enabled == true`

- Materializar bundle:

```text
POST /api/jobs/asset-bundles/{bundle_uid}/materialize
```

- Consultar estado/resultado:

```text
GET /api/jobs/asset-bundles/{bundle_uid}/materialization
```

- Obtener renderer manifest:

```text
GET /api/jobs/asset-bundles/{bundle_uid}/renderer-manifest
```

Mecanismo interno real:

- Usa `rclone copyto`.
- Lee `rclone_remote` y `remote_path` desde el asset o desde `selection_json`.
- Copia el master hacia filesystem local bajo `JOB_ASSETS_STORAGE_DIR`.
- Ruta default en stack principal: `/data/job-assets`.
- Estructura:

```text
${JOB_ASSETS_STORAGE_DIR}/{bundle_uid}/assets/{rank}_{asset_id}_{asset_uid}_{safe_filename}
${JOB_ASSETS_STORAGE_DIR}/{bundle_uid}/manifests/renderer-manifest.json
```

- Calcula SHA-256 del archivo ya copiado.
- Guarda por item:
  - `local_path`
  - `relative_path`
  - `materialized_size_bytes`
  - `materialized_sha256`
  - `materialized_at`
  - `materialization_status`
  - `materialization_error`

Request real de materializacion de bundle:

```json
{
  "force": false
}
```

Response real resumida:

```json
{
  "bundle_uid": "jab_xxx",
  "job_id": "mpt-job-001",
  "status": "ready",
  "materialization_status": "ready",
  "total_assets": 2,
  "materialized_assets": 2,
  "failed_assets": 0,
  "materialized_at": "2026-08-12T00:00:00Z",
  "materialized_assets_dir": "job-assets/jab_xxx",
  "renderer_manifest": {},
  "assets": [
    {
      "scene_id": "scene-001",
      "scene_index": 1,
      "rank": 1,
      "asset_id": 123,
      "asset_uid": "drive-52cee0a6",
      "filename": "video.mp4",
      "type": "video",
      "local_path": "/data/job-assets/jab_xxx/assets/1_123_drive-52cee0a6_video.mp4",
      "relative_path": "assets/1_123_drive-52cee0a6_video.mp4",
      "size_bytes": 123456,
      "sha256": "hex64",
      "materialization_status": "ready",
      "materialization_error": null,
      "score": 0.91,
      "match_reasons": [],
      "rclone_remote": "remote-name",
      "remote_path": "path/in/remote/video.mp4",
      "needs_human_review": false
    }
  ],
  "error": null
}
```

Sincrono/asyncrono:

- Es sincrono. El request HTTP ejecuta la copia en la misma llamada.
- Timeout interno de `rclone copyto`: `900` segundos por asset.
- No hay background job/queue.

Estados de bundle:

- `materialization_status`: observado en codigo/tests: `pending`/valor inicial por modelo, `processing`, `ready`, `partial`, `failed`.
- Item status: `processing`, `ready`, `failed`.

Idempotencia:

- Si el bundle ya esta `ready`, tiene `renderer_manifest_json`, y `force=false`, devuelve el resultado guardado y no vuelve a copiar.
- Si `force=true`, borra el directorio del bundle y rematerializa.
- Dentro de una misma ejecucion, assets duplicados en el bundle se copian una sola vez y reutilizan el mismo `relative_path`.

Estado actual pilot observado:

- `JOB_ASSET_MATERIALIZATION_ENABLED=false`.
- `JOB_ASSETS_STORAGE_DIR=/tmp/job-assets-disabled`.
- No hay volumen materializado compartido montado en el servicio pilot observado.
- Por tanto, en pilot la materializacion HTTP debe responder `503` con `"job asset materialization is disabled"` si se invoca.

### NO EXISTE

No existe hoy este contrato individual:

```json
{
  "asset_id": 123,
  "status": "ready",
  "local_path": "/ruta/visible/para/mpt/video.mp4"
}
```

Tampoco existe hoy este contrato individual:

```json
{
  "asset_id": 123,
  "status": "ready",
  "download_url": "http://servicio-interno/..."
}
```

No existe endpoint HTTP de descarga de masters materializados por asset.

### Contrato MPT

MPT no debe usar Drive/rclone directamente para descargar masters. El flujo soportado es:

1. `POST /api/assets/search`.
2. Elegir `asset_uid`.
3. `POST /api/jobs/asset-bundles` con `selected_asset_uids`.
4. `POST /api/jobs/asset-bundles/{bundle_uid}/materialize`.
5. `GET /api/jobs/asset-bundles/{bundle_uid}/renderer-manifest`.

El renderer manifest es la fuente para `asset_uid`, `local_path`, `relative_path`, `size_bytes` y `sha256`.

## Filesystem / Docker

### EXISTE HOY

Stack principal declarado:

- Path dentro del container Asset Hub:

```text
/data/job-assets
```

- Variable:

```text
JOB_ASSETS_STORAGE_DIR
```

- Volumen Docker declarado:

```text
job_assets:/data/job-assets
```

- Nombre Swarm real esperado del volumen:

```text
kurukin-asset-hub_job_assets
```

- Renderer/MPT puede montar:

```text
kurukin-asset-hub_job_assets:/data/job-assets:ro
```

Manifest renderer declara:

```json
{
  "storage": {
    "storage_dir": "/data/job-assets/{bundle_uid}",
    "assets_dir": "/data/job-assets/{bundle_uid}/assets",
    "manifests_dir": "/data/job-assets/{bundle_uid}/manifests",
    "base_path": "/data/job-assets",
    "path_mode": "container"
  }
}
```

Servicio pilot observado:

- Monta rclone config read-only.
- Monta previews pilot read-only.
- No monta volumen de job assets.
- Materializacion deshabilitada.

UID/GID/permisos:

- Dockerfile usa usuario default root.
- No hay `USER` no-root.
- Los archivos materializados por API/CLI se escriben previsiblemente como root dentro del contenedor.
- No hay configuracion explicita de UID/GID para compartir con MPT.

### Confirmacion sobre `/data/kurukin-assets/<asset_id>/...`

NO EXISTE hoy esa ruta.

El contrato MPT no agrega cache individual. El codigo usa la ruta de materializacion por bundle:

```text
/data/job-assets/{bundle_uid}/assets/...
```

## Lifecycle

### EXISTE HOY

Para bundle materializado:

- Queda cacheado en `JOB_ASSETS_STORAGE_DIR`.
- No hay TTL.
- No hay garbage collector.
- No hay eliminacion automatica.
- Puede reutilizarse si el bundle esta `ready`, tiene `renderer_manifest_json`, y `force=false`.
- `force=true` borra el directorio del bundle completo antes de copiar de nuevo.
- La copia se considera completa cuando:
  - `rclone copyto` termino sin error.
  - `local_path.stat().st_size` se pudo leer.
  - `compute_sha256(local_path)` termino.
  - item queda `materialization_status="ready"`.
- El renderer manifest incluye `file_exists`.

Riesgo durante render:

- Si MPT usa un bundle ya `ready` y nadie ejecuta `force=true` sobre ese mismo bundle, no hay proceso interno que lo borre.
- Si alguien ejecuta `force=true` durante un render que lee ese mismo path, puede desaparecer o cambiar el directorio.
- No hay lock de lectura/render.

### NO EXISTE

- No existe TTL configurable.
- No existe API de delete/purge cache.
- No existe lease/pin de render.
- No existe lock distribuido para impedir `force=true` durante render.

### RECOMENDACION

- MPT debe consumir solo materializaciones `ready`.
- Evitar `force=true` sobre bundles en uso.
- Agregar `cache_status`, `expires_at` opcional y `in_use/lease` si se automatiza limpieza.

## Errores y retry

### EXISTE HOY

Autenticacion:

- Credencial ausente: `401 {"detail":"API key required"}`. No retryable sin corregir credencial.
- Credencial invalida: `403 {"detail":"Invalid API key"}`. No retryable sin corregir credencial.

`POST /api/assets/search`:

- Query sin resultados: `200`, body con `count: 0`, `assets: []`. No es error; retryable solo si se espera que cambie el catalogo.
- `source_policy.sources=[]`: `422` validation error. No retryable sin cambiar request.
- `scope` invalido: `422` validation error. No retryable sin cambiar request.
- `scope=brand` sin `brand`: `422` validation error. No retryable sin cambiar request.
- `scope=title` sin `title`: `422` validation error. No retryable sin cambiar request.
- `limit < 1` o `limit > 200`: `422`. No retryable sin cambiar request.

Materializacion bundle:

- Bundle no encontrado: `404 {"detail":"Bundle not found"}`. No retryable salvo que el bundle se cree despues.
- Materializacion deshabilitada: `503 {"detail":"job asset materialization is disabled"}`. Retryable solo despues de cambiar config.
- `RCLONE_CONFIG` ausente/no archivo: `503 {"detail":"rclone is not configured for materialization"}`. Retryable despues de montar config.
- Bundle sin assets: `422 {"detail":"Bundle has no selected assets"}`. No retryable sin cambiar bundle.
- `bundle_uid` invalido/path invalido: `422`. No retryable sin cambiar request.
- Asset sin rclone remote/path: item queda `failed`; bundle `partial` o `failed`. No retryable sin arreglar metadata.
- Storage/rclone no disponible: item queda `failed`; bundle `partial` o `failed`. Retryable, idealmente con `force=true` despues de resolver storage.
- Timeout rclone: item queda `failed` con mensaje sanitizado; bundle `partial` o `failed`. Retryable.
- Error inesperado no capturado por API: FastAPI devolvera `500`. Retryable segun causa.

### NO EXISTE

- No hay codigos especificos para asset individual no encontrado porque no existe endpoint individual.
- No hay envelope estandar de errores con `code`, `retryable`, `request_id`.

### RECOMENDACION

Agregar errores normalizados:

```json
{
  "error": {
    "code": "storage_unavailable",
    "message": "storage temporarily unavailable",
    "retryable": true
  }
}
```

## Concurrencia

### EXISTE HOY

Materializacion por bundle:

- Dentro de un mismo request, si dos escenas usan el mismo asset, se copia una sola vez y se reutiliza `relative_path`.
- Si el bundle ya esta `ready` y `force=false`, una segunda llamada reutiliza el resultado guardado.

### NO EXISTE

- No hay locking DB/filesystem visible para dos requests simultaneos materializando el mismo `bundle_uid`.
- No hay locking/cache/idempotencia por `asset_id` individual porque no existe materializacion individual.
- Dos requests simultaneos con `force=true` podrian interferir sobre el mismo directorio.
- Dos requests simultaneos iniciales sobre un bundle no-ready podrian intentar escribir los mismos paths.

### RECOMENDACION

- Agregar lock por `bundle_uid` para bundle materialization.
- Usar archivo temporal por request y rename atomico.
- Registrar estado `processing` con owner/request id y timeout.

## Ejemplos reales curl

Usar una variable de entorno local, sin credencial hardcodeada:

```bash
export ASSET_HUB_BASE_URL="https://assets.kuruk.in"
export ASSET_HUB_API_KEY="REEMPLAZAR_EN_ENTORNO_LOCAL"
```

Generic solamente:

```bash
curl -sS -X POST "$ASSET_HUB_BASE_URL/api/assets/search" \
  -H "Content-Type: application/json" \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  -d '{
    "query": "atardecer",
    "limit": 5,
    "source_policy": {
      "sources": [
        {"scope": "generic"}
      ]
    }
  }'
```

Una serie/title solamente:

```bash
curl -sS -X POST "$ASSET_HUB_BASE_URL/api/assets/search" \
  -H "Content-Type: application/json" \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  -d '{
    "query": "telefono",
    "limit": 5,
    "source_policy": {
      "sources": [
        {"scope": "title", "title": "mi-otra-yo"}
      ]
    }
  }'
```

Title + generic:

```bash
curl -sS -X POST "$ASSET_HUB_BASE_URL/api/assets/search" \
  -H "Content-Type: application/json" \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  -d '{
    "query": "telefono",
    "limit": 10,
    "source_policy": {
      "sources": [
        {"scope": "title", "title": "mi-otra-yo"},
        {"scope": "generic"}
      ]
    }
  }'
```

Brand solamente:

```bash
curl -sS -X POST "$ASSET_HUB_BASE_URL/api/assets/search" \
  -H "Content-Type: application/json" \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  -d '{
    "query": "energia magica",
    "limit": 5,
    "source_policy": {
      "sources": [
        {"scope": "brand", "brand": "grandiosa-mujer"}
      ]
    }
  }'
```

Busqueda de videos:

```bash
curl -sS -X POST "$ASSET_HUB_BASE_URL/api/assets/search" \
  -H "Content-Type: application/json" \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  -d '{
    "query": "video atardecer",
    "limit": 5,
    "source_policy": {
      "sources": [
        {"scope": "generic"}
      ]
    }
  }'
```

Nota: `POST /api/assets/search` no tiene filtro `media_type`; este ejemplo solo busca texto. En la base pilot observada los assets elegibles son videos.

Busqueda de imagenes:

```bash
curl -sS -X POST "$ASSET_HUB_BASE_URL/api/assets/search" \
  -H "Content-Type: application/json" \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  -d '{
    "query": "imagen poster",
    "limit": 5,
    "source_policy": {
      "sources": [
        {"scope": "generic"}
      ]
    }
  }'
```

Nota: `POST /api/assets/search` no puede filtrar `media_type=image`; si no hay imagenes elegibles que matcheen texto, responde `count: 0`.

Crear bundle explicito:

```bash
curl -sS -X POST "$ASSET_HUB_BASE_URL/api/jobs/asset-bundles" \
  -H "Content-Type: application/json" \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  -d '{
    "job_id": "mpt-001",
    "created_by": "money-printer-turbo",
    "scenes": [
      {
        "scene_id": "scene-001",
        "scene_index": 1,
        "script_scene": "Escena uno",
        "selected_asset_uids": ["drive-52cee0a6"]
      }
    ]
  }'
```

Materializar bundle:

```bash
curl -sS -X POST "$ASSET_HUB_BASE_URL/api/jobs/asset-bundles/jab_xxx/materialize" \
  -H "Content-Type: application/json" \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}" \
  -d '{"force": false}'
```

Obtener renderer manifest:

```bash
curl -sS "$ASSET_HUB_BASE_URL/api/jobs/asset-bundles/jab_xxx/renderer-manifest" \
  -H "X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}"
```

## Fixtures para desarrollo

### EXISTE HOY

La base pilot local contiene estos assets elegibles al momento de la auditoria. Son utiles para pruebas locales/offline si se conserva una copia/snapshot de la DB pilot; no garantizan disponibilidad futura del binario en Drive.

| DB id | asset_uid / `asset_id` de search | scope | brand | title_slug | media_type | filename |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `drive-52cee0a6` | generic | null | null | video | `hombre-sentado-en-escritorio-trabajando-y-observando-la-pantalla-del-portatil__16x9__52cee0a6.mp4` |
| 2 | `drive-f78b5e8f` | brand | `grandiosa-mujer` | null | video | `grandiosa-mujer-intercambio-de-energia-magica-entre-dos-manos__9x16__f78b5e8f.mp4` |
| 4 | `drive-d3fd4797` | generic | null | null | video | `silueta-de-una-persona-de-pie-frente-a-un-atardecer__4x5__d3fd4797.mp4` |
| 5 | `drive-8c176338` | brand | `grandiosa-mujer` | null | video | `grandiosa-mujer-escena-magica-con-velas-fotos-antiguas-y-una-puerta-iluminada__9x16__8c176338.mp4` |
| 7 | `drive-422ede5f` | generic | null | null | video | `montanas-nevadas-al-atardecer__4x5__422ede5f.mp4` |

Title fixture existente:

- `title_slug`: `mi-otra-yo`

Brand fixture existente:

- `brand`: `grandiosa-mujer`

### NO EXISTE

- No existe carpeta de fixtures versionada con binarios locales.
- No existe contrato de "test asset" permanente que no dependa de Drive/storage.

### RECOMENDACION

- Crear 3-5 assets fixture versionados o materializados en un volumen local de desarrollo.
- Marcar sus `asset_uid` como reservados y no borrar/reingestar.

## Versionado

### EXISTE HOY

- Version de app FastAPI: `0.1.0`.
- Renderer manifest: `manifest_version: "1.0"`.
- `generated_by`: `kurukin-asset-hub`.
- Revision Alembic en DB pilot observada: `202608040002`.
- Schema renderer manifest en repo:
  - `app/schemas/renderer_manifest.py`
- Endpoint schema renderer manifest documentado en README:
  - `GET /api/renderer-manifest/schema`
- OpenAPI/Swagger existe por defaults de FastAPI:
  - `/openapi.json`
  - `/docs`
  - `/redoc`

### NO EXISTE

- No existe version explicita de `POST /api/assets/search`.
- No existe `/api/v1`.
- No existe documento OpenAPI custom versionado en repo.
- No hay politica formal escrita de backwards compatibility para `POST /api/assets/search`.

### RECOMENDACION

- Declarar `POST /api/assets/search` como contrato `v1`.
- Versionar nuevos campos de materializacion sin romper nombres existentes.
- Agregar tests contractuales especificos para MPT.

## Minimum contract MoneyPrinterTurbo can rely on

### EXISTE HOY

MPT puede confiar hoy en esto para busqueda:

```http
POST /api/assets/search
X-Asset-Hub-Api-Key: ${ASSET_HUB_API_KEY}
Content-Type: application/json
```

Request minimo:

```json
{
  "query": "texto opcional",
  "limit": 20,
  "source_policy": {
    "sources": [
      {"scope": "generic"}
    ]
  }
}
```

Response minimo:

```json
{
  "query": "texto opcional",
  "source_policy": {
    "sources": [
      {"scope": "generic", "brand": null, "title": null}
    ]
  },
  "count": 1,
  "assets": [
    {
      "asset_id": "drive-52cee0a6",
      "asset_uid": "drive-52cee0a6",
      "drive_file_id": "string-or-null",
      "scope": "generic",
      "brand": null,
      "collection": null,
      "title_type": null,
      "title": null,
      "title_context": null,
      "filename": "video.mp4",
      "target_path": "remote/path/video.mp4",
      "media_type": "video",
      "orientation": "16:9",
      "primary_theme": "string-or-null",
      "primary_topic": "string-or-null",
      "tags": []
    }
  ]
}
```

MPT debe almacenar para dedupe:

```text
kurukin_media:<asset_uid>
```

Ejemplo:

```text
kurukin_media:drive-52cee0a6
```

Para multiples sources, MPT puede confiar en que son OR, no cuotas.

### Materializacion

MPT NO debe usar un contrato directo `search result -> local_path/download_url`.

La materializacion existente es por Job Asset Bundle y requiere que MPT consuma:

- `POST /api/jobs/asset-bundles` con `selected_asset_uids`
- `POST /api/jobs/asset-bundles/{bundle_uid}/materialize`
- `GET /api/jobs/asset-bundles/{bundle_uid}/renderer-manifest`
- volumen compartido con `local_path`

El renderer manifest expone `asset_uid`, `local_path`, `relative_path`, `size_bytes` y `sha256`.
