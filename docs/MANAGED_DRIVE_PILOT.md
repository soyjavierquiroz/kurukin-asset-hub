# Managed Drive Pilot

## Arquitectura

El piloto agrega un flujo secuencial para assets nuevos en Google Drive. No usa workers legacy, no recupera dumps legacy y no mueve assets antiguos.

La base operativa del piloto debe ser un PostgreSQL aislado:

```bash
docker compose --env-file .env.pilot -f docker-compose.pilot.yml up -d pilot-db
```

Servicio y volumen:

```text
servicio: kurukin-asset-hub-pilot-db
base: kurukin_asset_hub_pilot
volumen: kurukin_asset_hub_pilot_postgres_data
```

El puerto de desarrollo, si se publica, debe quedar enlazado a `127.0.0.1`. No restaurar dumps legacy ni compartir `PGDATA` con la base corrupta.

Flujo:

1. Lista o consulta un archivo por `drive_file_id`.
2. Descarga el archivo a un temporal local.
3. Calcula checksum y MIME.
4. Extrae metadata técnica con `ffprobe`.
5. Genera previews en `PILOT_PREVIEW_ROOT`.
6. Reutiliza el esquema/prompts/proveedor de AI enrichment existente cuando `AI_ENRICHMENT_ENABLED=true`.
7. Mapea la respuesta IA a taxonomía controlada.
8. Calcula nombre y ruta final.
9. En dry-run persiste el plan sin mover Drive.
10. Con `--apply`, crea carpetas lazy, renombra, mueve y verifica el mismo `drive_file_id`.
11. Registra o actualiza `assets` por `drive_file_id` para evitar duplicados.

El cliente Drive nuevo es `GoogleDriveAPIClient` en `app/services/managed_drive_pilot.py`. Usa Google Drive API v3 por ID estable. La suite usa fakes y no toca Drive real.

## Autenticación

Modo recomendado:

```env
GOOGLE_DRIVE_AUTH_MODE=adc
GOOGLE_APPLICATION_CREDENTIALS=/ruta/protegida/credentials.json
```

`adc` usa Application Default Credentials de `google-auth`; la librería refresca las credenciales antes de las peticiones. La service account debe tener acceso explícito a las carpetas piloto.

Modo temporal:

```env
GOOGLE_DRIVE_AUTH_MODE=access_token
GOOGLE_DRIVE_ACCESS_TOKEN=
```

`access_token` sirve solo para pruebas manuales cortas. No es renovable por el piloto y no es el modo recomendado.

## Scopes

Cada asset tiene exactamente un scope:

- `generic`: `brand_id` y `title_name` nulos.
- `brand`: exige `brand_id`; no puede tener título.
- `title`: exige `title_name` y `title_type`.

La IA no decide el scope. El scope viene del comando, de la source o de la carpeta administrada.

## Estructura Drive

Raíz piloto:

```text
KURUKIN_ASSET_HUB_PILOT/
├── 00_entrada/
│   ├── genericos/
│   ├── marcas/{marca}/
│   └── peliculas-series/{titulo}/
├── 10_genericos/
├── 20_marcas/
├── 30_peliculas_series/
├── 90_revision/
└── 99_errores/
```

Las subcarpetas finales se crean lazy durante `--apply`.

## CLI

Preflight de solo lectura:

```bash
asset-hub drive preflight \
  --source-folder-id "$GOOGLE_DRIVE_GENERIC_INBOX_FOLDER_ID" \
  --destination-root-id "$GOOGLE_DRIVE_ROOT_FOLDER_ID" \
  --file-id "$GENERIC_FILE_ID"
```

El preflight valida autenticación, carpetas, Drive compartido/My Drive, capacidades declaradas por Drive, raíz piloto esperada y configuración de scopes. No crea carpetas, no mueve archivos, no renombra y no escribe `appProperties`.

Dry-run genérico:

```bash
asset-hub drive ingest \
  --source-folder-id "$GOOGLE_DRIVE_GENERIC_INBOX_FOLDER_ID" \
  --destination-root-id "$GOOGLE_DRIVE_ROOT_FOLDER_ID" \
  --scope generic \
  --file-id "$GENERIC_FILE_ID" \
  --dry-run
```

Marca:

```bash
asset-hub drive ingest \
  --source-folder-id "$GOOGLE_DRIVE_BRAND_INBOX_FOLDER_ID" \
  --destination-root-id "$GOOGLE_DRIVE_ROOT_FOLDER_ID" \
  --scope brand \
  --brand grandiosa-mujer \
  --collection evergreen \
  --file-id "$BRAND_FILE_ID" \
  --dry-run
```

Película:

```bash
asset-hub drive ingest \
  --source-folder-id "$GOOGLE_DRIVE_TITLE_INBOX_FOLDER_ID" \
  --destination-root-id "$GOOGLE_DRIVE_ROOT_FOLDER_ID" \
  --scope title \
  --title-type movie \
  --title "Titulo de prueba" \
  --file-id "$TITLE_FILE_ID" \
  --dry-run
```

Serie:

```bash
asset-hub drive ingest \
  --source-folder-id "$GOOGLE_DRIVE_TITLE_INBOX_FOLDER_ID" \
  --destination-root-id "$GOOGLE_DRIVE_ROOT_FOLDER_ID" \
  --scope title \
  --title-type series \
  --title "Nombre de la serie" \
  --season 1 \
  --episode 2 \
  --limit 10
```

Aplicar sobre un archivo:

```bash
asset-hub drive ingest \
  --source-folder-id "$GOOGLE_DRIVE_GENERIC_INBOX_FOLDER_ID" \
  --destination-root-id "$GOOGLE_DRIVE_ROOT_FOLDER_ID" \
  --scope generic \
  --file-id "$FILE_ID" \
  --apply
```

`--apply` es obligatorio para mover. El modo por defecto es dry-run.

## Dry-Run

El dry-run descarga, analiza, genera preview, ejecuta enrichment si está habilitado, calcula destino y persiste el plan en PostgreSQL piloto. No renombra, no mueve, no crea carpetas finales en Drive, no incrementa `item_count`, no marca `move_status=moved` y no marca el asset como `ready`.

Estado esperado:

```text
status=move_planned
move_status=planned
drive_mutations_performed=0
```

La salida es JSON con:

- `drive_file_id`
- `original_name`
- `original_parent_id`
- `source_drive_id`
- `scope`
- `brand`
- `title`
- `collection`
- `mime_type`
- metadata técnica
- orientación
- `title_es`
- `description_es`
- tema/subtema
- tags/usos sugeridos
- reglas de flip/zoom/texto/logo
- `target_path`
- `target_name`
- `target_batch`
- `preview_path`
- `thumbnail_path`
- `classification_confidence`
- `requires_review`
- `database_status`
- `drive_mutations_performed`
- warnings

## Apply y Rollback

Antes de mover se guardan:

- `drive_file_id`
- `original_parent_id`
- `original_name`
- `target_parent_id`
- `target_name`

Después de mover se verifica mismo ID, nuevo nombre y nuevo padre. Si falla el movimiento o verificación, el piloto intenta restaurar nombre y padre originales. Si no puede, deja `move_status=rollback_required` y `status=review_required`.

## Variables

Usar una base limpia:

```env
DATABASE_URL=
PILOT_PREVIEW_ROOT=
```

Para ejecutar el piloto, `DATABASE_URL` debe apuntar a `kurukin_asset_hub_pilot`. No apuntar producción a esa base.

Variables Drive/previews:

```env
GOOGLE_DRIVE_AUTH_MODE=adc
GOOGLE_APPLICATION_CREDENTIALS=
GOOGLE_DRIVE_ROOT_FOLDER_ID=
GOOGLE_DRIVE_GENERIC_INBOX_FOLDER_ID=
GOOGLE_DRIVE_BRAND_INBOX_FOLDER_ID=
GOOGLE_DRIVE_TITLE_INBOX_FOLDER_ID=
GOOGLE_DRIVE_GENERIC_LIBRARY_FOLDER_ID=
GOOGLE_DRIVE_BRAND_LIBRARY_FOLDER_ID=
GOOGLE_DRIVE_TITLE_LIBRARY_FOLDER_ID=
GOOGLE_DRIVE_REVIEW_FOLDER_ID=
GOOGLE_DRIVE_ERROR_FOLDER_ID=
MANAGED_FOLDER_BATCH_SIZE=250
PILOT_PREVIEW_ROOT=/var/lib/kurukin-asset-hub-pilot/previews
AI_ENRICHMENT_ENABLED=false
```

Crear `.env.pilot` desde `.env.pilot.example` sin copiar secretos desde `.env` legacy.

## Prueba Manual

Crear carpetas nuevas en Drive:

```text
00_entrada/genericos
00_entrada/marcas/grandiosa-mujer
00_entrada/peliculas-series/titulo-de-prueba
```

Subir un archivo nuevo y prescindible por scope. Primero ejecutar preflight y luego dry-run. Revisar scope, marca/título, orientación, tema, subtema, nombre, ruta, extensión, sufijo, preview, metadata técnica, duplicados y que `drive_mutations_performed=0`.

No ejecutar `--apply` hasta aprobación posterior. El primer apply aprobado debe ser un único archivo genérico prescindible.

## Estados

Estados de asset del piloto:

```text
discovered, downloading, technical_analysis, preview_generation, ai_analysis,
move_planned, moving, moved, ready, review_required, failed
```

Estados de movimiento:

```text
not_planned, planned, moving, moved, rollback_required, move_failed
```

## Fuera Del Piloto

- Recuperación de la base legacy.
- Workers legacy.
- Movimientos de assets antiguos.
- Concurrencia.
- Búsqueda vectorial nueva.
- Motor complejo de permisos.
- Borrado o papelera en Drive.
