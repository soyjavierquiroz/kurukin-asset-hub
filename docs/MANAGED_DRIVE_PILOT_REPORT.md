# Managed Drive Pilot Report

## Resultado

Endurecido el piloto limpio de ingestión y organización administrada en Google Drive para assets nuevos. La base legacy no fue restaurada ni migrada; solo se consultó su versión de Alembic en modo lectura para confirmar que sigue en `202607220002`.

## Rama y Base

- Rama: `feature/managed-drive-pilot`
- Commit base auditado: `53348dd0ddf1bd77ba8a327414457f5df801a62d`
- Base piloto operativa: `kurukin_asset_hub_pilot`
- Servicio piloto: `kurukin-asset-hub-pilot-db`
- Volumen piloto: `kurukin_asset_hub_pilot_postgres_data`
- Alembic desde cero: `202608030001 (head)`
- Alembic legacy: `202607220002`

## Reutilización

- Se reutiliza `assets` para identidad, metadata, previews, marca y estado.
- Se reutiliza `brands` para assets de marca.
- Se reutiliza `AssetAIAnalysis`, el prompt/schema existente y el proveedor de AI enrichment.
- Se reutilizan helpers existentes de `ffprobe`, `ffmpeg` y sanitización de errores.
- No se creó microservicio ni worker nuevo.

## Implementación

Archivos creados:

- `.env.pilot.example`
- `docker-compose.pilot.yml`
- `app/models/managed_drive_folder.py`
- `app/services/managed_drive_pilot.py`
- `scripts/asset_hub.py`
- `scripts/__init__.py`
- `tests/test_managed_drive_pilot.py`
- `alembic/versions/202608030001_managed_drive_pilot.py`
- `docs/MANAGED_DRIVE_PILOT.md`
- `docs/MANAGED_DRIVE_PILOT_REPORT.md`

Archivos modificados:

- `.gitignore`
- `app/config.py`
- `app/models/asset.py`
- `app/models/__init__.py`
- `app/schemas/ai_enrichment.py`
- `app/services/ai_prompts.py`
- `app/services/asset_preview.py`
- `app/api/assets.py`
- `.env.example`
- `pyproject.toml`

## Pruebas

Comandos ejecutados:

```bash
docker compose --env-file .env.pilot -f docker-compose.pilot.yml up -d pilot-db
set -a; . ./.env.pilot; set +a; .venv/bin/python -m alembic upgrade head
set -a; . ./.env.pilot; set +a; .venv/bin/python -m alembic current
set -a; . ./.env.pilot; set +a; .venv/bin/python -m alembic heads
set -a; . ./.env.pilot; set +a; .venv/bin/python -m alembic check
set -a; . ./.env.pilot; set +a; .venv/bin/python -m alembic upgrade head
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/asset-hub --help
.venv/bin/asset-hub drive --help
.venv/bin/asset-hub drive ingest --help
.venv/bin/asset-hub drive preflight --help
.venv/bin/python -m py_compile $(rg --files app scripts tests -g '*.py')
.venv/bin/ruff check app scripts tests
.venv/bin/python -m pytest -q
```

Resultados:

- Tests nuevos/focalizados del piloto: `34 passed`
- Tests focalizados piloto + API: `59 passed, 1 warning`
- Suite completa: `246 passed, 1 warning`
- Ruff: `All checks passed`
- Py compile: OK
- Warning restante: `StarletteDeprecationWarning` de `fastapi.testclient` por transición a `httpx2`; no proviene del piloto.
- Alembic desde cero e idempotencia: OK

## Drive

El cliente elegido es Google Drive API v3 mínimo, porque el flujo necesita operar por `drive_file_id`, mover por ID y verificar padre/nombre después del movimiento. Rclone se conserva para el sistema existente, pero no se duplicó dentro del piloto.

Autenticación:

- Predeterminado: `GOOGLE_DRIVE_AUTH_MODE=adc`.
- Temporal: `GOOGLE_DRIVE_AUTH_MODE=access_token`.
- El token manual no se imprime ni se registra.
- ADC usa `google-auth` y soporta `GOOGLE_APPLICATION_CREDENTIALS`.

Operaciones soportadas:

- `list_folder`
- `get_file_metadata`
- `download_file`
- `create_folder`
- `rename_and_move_file`
- `restore_file_location`
- `verify_file_location`
- `preflight_drive_access`

Preflight:

```text
AUTHENTICATION: OK
AUTH_MODE: adc
SOURCE_FOLDER: pendiente de IDs reales
SOURCE_DRIVE: pendiente de IDs reales
DESTINATION_FOLDER: pendiente de IDs reales
DESTINATION_DRIVE: pendiente de IDs reales
SAME_DRIVE: pendiente de IDs reales
CAN_LIST_SOURCE: pendiente de IDs reales
CAN_CREATE_IN_DESTINATION: pendiente de IDs reales
CAN_MOVE_FILES: pendiente de IDs reales
DRIVE_MUTATIONS_PERFORMED: 0
READY_FOR_DRY_RUN: NO
```

Estado real: `WAITING_FOR_PILOT_CREDENTIALS`.

La suite usa `FakeDrive`; no ejecuta movimientos reales.

## Dry-Run Real

No se ejecutó dry-run real porque faltan credenciales ADC/Drive IDs y `GENERIC_FILE_ID`, `BRAND_FILE_ID`, `TITLE_FILE_ID`.

Comandos preparados:

```bash
asset-hub drive ingest --source-folder-id "$GOOGLE_DRIVE_GENERIC_INBOX_FOLDER_ID" --destination-root-id "$GOOGLE_DRIVE_ROOT_FOLDER_ID" --scope generic --file-id "$GENERIC_FILE_ID" --dry-run
asset-hub drive ingest --source-folder-id "$GOOGLE_DRIVE_BRAND_INBOX_FOLDER_ID" --destination-root-id "$GOOGLE_DRIVE_ROOT_FOLDER_ID" --scope brand --brand grandiosa-mujer --collection evergreen --file-id "$BRAND_FILE_ID" --dry-run
asset-hub drive ingest --source-folder-id "$GOOGLE_DRIVE_TITLE_INBOX_FOLDER_ID" --destination-root-id "$GOOGLE_DRIVE_ROOT_FOLDER_ID" --scope title --title-type movie --title "Titulo de prueba" --file-id "$TITLE_FILE_ID" --dry-run
```

Planes: pendientes hasta recibir IDs reales. El contrato de salida ya incluye `drive_mutations_performed=0`, metadata técnica, enrichment, ruta, nombre, lote, preview y estado DB.

## API Piloto

Instancia local levantada contra PostgreSQL piloto en `127.0.0.1:18080`, sin Traefik.

Validaciones:

- `/healthz`: OK.
- `/readyz`: OK.
- `/api/assets/search?scope=generic&limit=1` con API key piloto: OK, respuesta vacía porque aún no hay assets reales.
- `/api/assets/search?limit=1` sin API key: `401`.
- Tests confirman que `scope=brand&brand=...` no mezcla marcas, `scope=generic` no devuelve títulos y `scope=title&title=...` no mezcla títulos.

## Seguridad

- `.env.pilot` está ignorado por Git.
- Credenciales reales no se versionaron.
- No se imprimieron secretos ni tokens.
- No se ejecutó `--apply`.
- No se movieron, renombraron ni crearon carpetas en Google Drive.
- No se tocaron assets legacy.
- Web legacy sigue `0/0`.
- No hay worker legacy activo.

## Limitaciones

- No se ejecutó prueba manual real contra Drive porque no hay IDs reales de carpetas piloto ni token OAuth en el repo.
- Preview MP4 de video queda fuera del piloto inicial; se genera thumbnail WebP para video.
- Si `AI_ENRICHMENT_ENABLED=false`, el piloto usa una clasificación conservadora de fallback para permitir pruebas locales; con IA real habilitada reutiliza el provider existente.
- La app usa `DATABASE_URL`; para correr el piloto se debe exportar `DATABASE_URL` apuntando a la base piloto.

## Siguiente Paso

Crear o compartir la carpeta `KURUKIN_ASSET_HUB_PILOT`, cargar un archivo nuevo por scope, completar `.env.pilot` con IDs/credenciales ADC y ejecutar primero `asset-hub drive preflight`. Después correr los tres dry-run preparados. No ejecutar `--apply` hasta una misión posterior.
