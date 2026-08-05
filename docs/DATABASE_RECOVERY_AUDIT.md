# Database Recovery Audit

Fecha UTC: 2026-08-03

## 1. Estado de congelacion de escrituras

La base original `kurukin_asset_hub` fue tratada como evidencia y fuente de recuperacion. No se ejecuto `REINDEX`, `VACUUM FULL`, `DELETE`, `UPDATE`, `ALTER TABLE`, migraciones Alembic nuevas, sync apply, enrichment, segmentacion, materializacion ni operaciones sobre Google Drive.

Estado registrado antes de congelar:

- Rama: `feature/dual-llm-fallback`
- Commit: `53348dd0ddf1bd77ba8a327414457f5df801a62d`
- Imagen web: `kurukin-asset-hub-web:nvidia-fallback-53348dd`
- Imagen PostgreSQL: `postgres:16-alpine`
- Contenedores relevantes iniciales:
  - `kurukin-asset-hub_web.1.7pi9qfhhzypq55he88057z9nt`: `Up`, healthy
  - `kurukin-asset-hub_db.1.oh9ofl52ly2261mya30slwsri`: `Up`, healthy
- Servicios Swarm iniciales:
  - `kurukin-asset-hub_web`: `1/1`
  - `kurukin-asset-hub_db`: `1/1`
  - `asset-worker`: definido en `docker-compose.yml`, sin servicio Swarm desplegado activo

Variables de entorno observadas, solo nombres: `ADMIN_PASSWORD`, `ADMIN_USERNAME`, `AI_ENRICHMENT_ENABLED`, `AI_FRAME_SAMPLE_COUNT`, `AI_MAX_ASSETS_PER_BATCH`, `AI_MODEL`, `AI_PROVIDER`, `AI_REVIEW_THRESHOLD`, `APP_ENV`, `ASSET_HUB_API_KEY`, `ASSET_HUB_HOST`, `DATABASE_URL`, `DERIVED_ASSET_CATEGORY_FOLDERS`, `JOB_ASSETS_STORAGE_DIR`, `JOB_ASSET_MATERIALIZATION_ENABLED`, `LOG_LEVEL`, `LONG_VIDEO_SEGMENTATION_ENABLED`, `LONG_VIDEO_THRESHOLD_SECONDS`, `NVIDIA_API_KEY`, `OPENAI_API_KEY`, `POSTGRES_DB`, `POSTGRES_PASSWORD`, `POSTGRES_USER`, `PREVIEW_STORAGE_DIR`, `RCLONE_CONFIG`, `SEGMENT_*`, `STACK_NAME`.

Volumenes y mounts relevantes:

- PostgreSQL: `kurukin-asset-hub_asset_hub_postgres_data` en `/var/lib/postgresql/data`
- Previews: `kurukin-asset-hub_previews` en `/data/previews`
- Job assets: `kurukin-asset-hub_job_assets` en `/data/job-assets`
- Rclone: `/root/.config/rclone/rclone.conf` montado read-only en `/config/rclone/rclone.conf`

Congelacion aplicada:

- `docker service scale kurukin-asset-hub_web=0`
- `kurukin-asset-hub_web` quedo en `0/0`
- `kurukin-asset-hub_db` permanecio disponible, salvo durante la copia fisica consistente
- Se encontro una conexion residual idle con ultima sentencia `ROLLBACK`; se cerro con `pg_terminate_backend`. Despues: `pg_stat_activity` reporto `0` conexiones de aplicacion a la base.

## 2. Respaldos creados

Ubicacion protegida, fuera del repositorio:

`/opt/backups/kurukin-asset-hub/20260803-204010/`

Permisos aplicados: directorio `700`, archivos `600`.

| Archivo | Tamano | SHA-256 | Resultado |
| --- | ---: | --- | --- |
| `kurukin_asset_hub.dump` | 705298 bytes | `69b257affa01d761d347d9ca9189bae518a4a18f1679dece4e597748847a60d0` | Fallo durante `pg_dump` completo |
| `kurukin_asset_hub_schema.sql` | 62581 bytes | `d9677dde33de72c68073d9efb184b53db0de2b8083a379aa7b340d3e3c3fc03e` | OK |
| `kurukin_asset_hub_data.dump` | 45259 bytes | `7abd3ff2b52ea38e1b87e58626ed15654354cbfa38f2ac30fbe1c20cfb421180` | Fallo durante `pg_dump --data-only` |
| `kurukin_asset_hub_globals.sql` | 673 bytes | `3b1949dd94e0667fd9d8a263fc0f4547f08c6245ed47ff921fe42696021a3946` | OK; contiene configuracion global protegida, no impresa |
| `kurukin_asset_hub_pgdata.tar.gz` | 7962093 bytes | `0b6b084e226f32fe96d28ddb847d308758400a56730770aa5a12f5b3b5d149b4` | OK; copia fisica con PostgreSQL detenido |

Verificacion `pg_restore --list`:

- `kurukin_asset_hub.dump`: exit `0`; legible como archivo custom, pero incompleto/no confiable por fallo de `pg_dump`.
- `kurukin_asset_hub_data.dump`: exit `0`; legible como archivo custom, pero incompleto/no confiable por fallo de `pg_dump`.

## 3. Resultado de pg_dump

`schema-only` y globals terminaron correctamente.

El dump logico completo y el dump de datos fallaron al leer `public.assets`:

```text
pg_dump: error: Dumping the contents of table "assets" failed: PQgetResult() failed.
ERROR: missing chunk number 0 for toast value 18339 in pg_toast_16499
Command was: COPY public.assets (...) TO stdout;
```

No se intento reparar la base original.

## 4. PostgreSQL limpio de auditoria

Se levanto un PostgreSQL separado:

- Contenedor: `kurukin_asset_hub_audit`
- Imagen: `postgres:16-alpine`
- Volumen: `kurukin_asset_hub_audit_pgdata`
- Puerto host: `127.0.0.1:55432`
- Base: `kurukin_asset_hub_audit`
- No comparte `PGDATA` con produccion.

Primer intento de restauracion:

- Comando: `pg_restore --verbose --no-owner --no-privileges --exit-on-error --dbname=kurukin_asset_hub_audit /backup/kurukin_asset_hub.dump`
- Duracion aproximada: 1 segundo
- Warnings: 0
- Resultado: fallo
- Primer error: `pg_restore: error: could not read from input file: end of file`
- Objeto en proceso al fallar: datos de `public.assets`

No existe restauracion logica limpia utilizable para comparar contra la original.

## 5. Diagnostico de integridad fisica

`pg_amcheck` esta disponible: PostgreSQL `16.14`.

Ejecucion directa sobre la original:

- No se instalo `amcheck` en la original.
- `pg_amcheck` sobre la original no pudo verificar relaciones: la extension `amcheck` no estaba instalada.
- Resultado: `no relations to check`.

Ejecucion sobre clon fisico seguro:

- Clon desde `kurukin_asset_hub_pgdata.tar.gz`
- Contenedor: `kurukin_asset_hub_physical_clone`
- Volumen: `kurukin_asset_hub_physical_clone_pgdata`
- Puerto host: `127.0.0.1:55433`
- Se instalo `amcheck` solo en el clon.
- `pg_amcheck --database=kurukin_asset_hub --parent-check --verbose`
- Exit: `2`

Errores encontrados en el clon:

- Heap/TOAST: multiples errores en `pg_toast.pg_toast_16499`, por ejemplo `xmin ... equals or exceeds next valid transaction ID 0:8727`.
- Heap/TOAST adicional: errores en `pg_toast.pg_toast_16762`.
- B-tree: warning en `public.ix_assets_filename`: `btree checking function returned unexpected number of rows: 2`.
- Referencias rotas fisicas: evidencia indirecta por `missing chunk number 0 for toast value 18339 in pg_toast_16499`.
- Objetos no verificables en original: todas las relaciones por falta de extension `amcheck` en la base original.

## 6. Comparacion original versus restauracion limpia

No ejecutable: la restauracion logica completa fallo. La base limpia no contiene un catalogo restaurado confiable.

Conteos read-only en original:

| Tabla | Filas |
| --- | ---: |
| `assets` | 701 |
| `asset_keywords` | 13203 |
| `asset_ai_analyses` | 696 |
| `asset_segments` | 779 |
| `asset_segmentation_runs` | 25 |
| `asset_tags` | 114 |
| `raw_videos` | 23 |
| `sources` | 4 |
| `brands` | 3 |
| `products` | 2 |
| `job_asset_bundles` | 1 |
| `job_asset_bundle_items` | 4 |

Identidad de `assets`, normal y con `enable_indexscan=off; enable_bitmapscan=off`:

| Metrica | Normal | Seq/bitmap off |
| --- | ---: | ---: |
| Total filas | 701 | 701 |
| `COUNT(DISTINCT id)` | 697 | 697 |
| `COUNT(DISTINCT asset_uid)` | 697 | 697 |
| Duplicados por `id` | 4 grupos | 4 grupos |
| Duplicados por `asset_uid` | 4 grupos | 4 grupos |
| Duplicados por `(source_id, remote_path)` | 4 grupos | 4 grupos |
| Duplicados por `(source_id, remote_file_id)` | 0 | 0 |
| Duplicados por `drive_file_id` | 0 | 0 |
| Duplicados por `checksum` | 0 | 0 |

## 7. Duplicados encontrados

Duplicados por `id`, `asset_uid` y `(source_id, remote_path)`:

| id | count | asset_uid |
| ---: | ---: | --- |
| 40 | 2 | `asset_0c51852652d61ac9c58996bd-seg-006` |
| 42 | 2 | `asset_0c51852652d61ac9c58996bd-seg-008` |
| 47 | 2 | `asset_0c51852652d61ac9c58996bd-seg-013` |
| 54 | 2 | `asset_de3b87843efa1e1395f84e28-seg-005` |

Duplicados por `(source_id, remote_path)`:

| source_id | remote_path | count | ids |
| ---: | --- | ---: | --- |
| 8 | `couples/propuesta_de_matrimonio_segment_006.mp4` | 2 | `40,40` |
| 8 | `couples/propuesta_sorpresa_segment_008.mp4` | 2 | `42,42` |
| 8 | `men/hombre_tomando_cafe_segment_005.mp4` | 2 | `54,54` |
| 8 | `women/mujer_al_aire_libre_segment_013.mp4` | 2 | `47,47` |

## 8. Orfandades encontradas

Anti-joins read-only en original:

| Relacion | Orfandades |
| --- | ---: |
| `asset_segments.parent_asset_id` | 455 |
| `asset_segments.child_asset_id` | 15 |
| `asset_keywords.asset_id` | 0 |
| `asset_ai_analyses.asset_id` | 0 |
| `job_asset_bundle_items.bundle_id` | 0 |
| `job_asset_bundle_items.asset_id` | 0 |
| `assets.parent_asset_id` | 448 |
| `assets.brand_id` | 0 |
| `assets.product_id` | 0 |
| `assets.source_id` | 0 |
| `assets.raw_video_id` | 0 |

Las constraints declaradas incluyen FKs validadas para `asset_segments.parent_asset_id`, `asset_segments.child_asset_id` y `assets.parent_asset_id`; por tanto, las orfandades observadas contradicen el catalogo logico declarado y refuerzan la hipotesis de corrupcion fisica/catalogo.

## 9. Constraints e indices

Constraints en `public`:

- Total: 101
- Check: 31
- Foreign key: 31
- Primary key: 23
- Unique: 16
- Todas las constraints listadas aparecen con `convalidated=true`.

Indices en `public`:

- Total: 96
- Unicos: 39
- `indisvalid=true`: 96
- `indisready=true`: 96
- `indislive=true`: 96
- Tamano total aproximado: 3465216 bytes
- Diferencias original/restauracion limpia: no comparables porque no hubo restauracion logica limpia.
- Problema fisico detectado por `pg_amcheck` en clon: `public.ix_assets_filename`.

## 10. Tests contra base limpia

No ejecutados contra una base limpia restaurada porque la restauracion completa fallo. No se conectaron workers ni Google Drive en modo escritura. No se ejecutaron enrichment, segmentacion ni sync apply.

Health/readiness y endpoints de lectura contra la base limpia tampoco se ejecutaron porque no existe una restauracion completa confiable.

## 11. Seguridad

`.env` esta excluido por:

- `.gitignore:10`
- `.dockerignore:7`

`git ls-files -- .env .env.local .env.production` no mostro archivos trackeados. `git log --all -- .env .env.local .env.production` no mostro historial para esos paths.

Credenciales que deben rotarse despues de asegurar la recuperacion:

- `POSTGRES_PASSWORD`
- `ADMIN_PASSWORD`
- `ASSET_HUB_API_KEY`
- `OPENAI_API_KEY`
- `NVIDIA_API_KEY`
- Credenciales/tokens dentro de `/root/.config/rclone/rclone.conf`
- Cualquier secreto derivado de `DATABASE_URL`

No se realizo rotacion durante esta mision.

## 12. Clasificacion

Escenario C: corrupcion fisica amplia.

Motivos:

- `pg_dump` completo falla leyendo `public.assets`.
- `pg_dump --data-only` falla leyendo `public.assets`.
- Error exacto: `missing chunk number 0 for toast value 18339 in pg_toast_16499`.
- La restauracion limpia del dump incompleto falla con `end of file`.
- `pg_amcheck` en clon fisico encuentra errores de heap/TOAST y un warning B-tree.
- Hay duplicados de claves que deberian estar protegidas por constraints unicas.
- Hay orfandades que contradicen FKs declaradas como validadas.

## 13. Recomendacion concreta

Recuperar por tablas, exportar filas legibles y reconstruir el catalogo en una base nueva. No sustituir produccion por una restauracion logica porque no existe dump completo confiable.

Siguiente fase recomendada, sin tocar la base original:

1. Mantener escrituras congeladas.
2. Trabajar desde el clon fisico y/o exports read-only.
3. Exportar tablas no afectadas con `COPY`/`pg_dump --table`.
4. Para `assets`, exportar filas legibles por rangos de `id` y columnas, aislando columnas TOAST corruptas.
5. Definir reglas explicitas de deduplicacion y cuarentena para `id`, `asset_uid`, `(source_id, remote_path)` y relaciones de segmentacion.
6. Restaurar en una base nueva con constraints activas.
7. Ejecutar los 209 tests, health/readiness y endpoints de lectura contra esa nueva base.
8. Rotar credenciales despues de asegurar respaldo y validacion.

## 14. Riesgos pendientes

- No hay dump logico completo confiable.
- Parte de `assets` contiene TOAST ilegible.
- Las FKs declaradas no coinciden con anti-joins observados.
- Puede haber mas corrupcion en columnas TOAST no leidas por consultas ligeras.
- El clon fisico fue modificado solo para instalar `amcheck`; no debe usarse como reemplazo productivo.
- Produccion queda congelada operacionalmente; antes de reanudar trafico debe decidirse si mantener mantenimiento o avanzar a recuperacion controlada.

## 15. Comandos ejecutados, sanitizados

```bash
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --short
docker ps --format '...'
docker service ls --format '...'
docker inspect <web-container> --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -E 's/=.*$/=***/'
docker inspect <db-container> --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -E 's/=.*$/=***/'
docker service scale kurukin-asset-hub_web=0
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'select ... from pg_stat_activity ...'
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'select pg_terminate_backend(pid) ...'
pg_dump -U "$POSTGRES_USER" --format=custom --verbose --no-owner --no-privileges "$POSTGRES_DB"
pg_dump -U "$POSTGRES_USER" --schema-only --no-owner --no-privileges "$POSTGRES_DB"
pg_dump -U "$POSTGRES_USER" --data-only --format=custom --verbose --no-owner --no-privileges "$POSTGRES_DB"
pg_dumpall -U "$POSTGRES_USER" --globals-only
docker service scale kurukin-asset-hub_db=0
docker run --rm -v kurukin-asset-hub_asset_hub_postgres_data:/pgdata:ro -v "$BACKUP_DIR":/backup alpine:3.20 sh -lc 'cd /pgdata && tar -czf /backup/kurukin_asset_hub_pgdata.tar.gz .'
docker service scale kurukin-asset-hub_db=1
sha256sum "$BACKUP_DIR"/*
pg_restore --list /backup/kurukin_asset_hub.dump
pg_restore --list /backup/kurukin_asset_hub_data.dump
docker run -d --name kurukin_asset_hub_audit ... postgres:16-alpine
createdb -U audit_user kurukin_asset_hub_audit
pg_restore --verbose --no-owner --no-privileges --exit-on-error --dbname=kurukin_asset_hub_audit /backup/kurukin_asset_hub.dump
pg_amcheck -U "$POSTGRES_USER" --database="$POSTGRES_DB" --all --parent-check --verbose
docker run -d --name kurukin_asset_hub_physical_clone ... postgres:16-alpine
psql -U asset_hub -d kurukin_asset_hub -c 'CREATE EXTENSION IF NOT EXISTS amcheck;'
pg_amcheck -U asset_hub --database=kurukin_asset_hub --parent-check --verbose
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /tmp/kurukin_diag.sql
git check-ignore -v .env .env.local .env.production
git ls-files -- .env .env.local .env.production
git log --all -- .env .env.local .env.production
```

## 16. Confirmacion de no modificacion destructiva

No se ejecutaron operaciones destructivas o reparadoras sobre la base original. Las unicas acciones operacionales sobre la original fueron congelar servicios, cerrar una conexion residual idle, ejecutar backups, detener PostgreSQL limpiamente para copia fisica, reiniciarlo y hacer consultas diagnosticas read-only. La extension `amcheck` se instalo solo en un clon fisico separado.
