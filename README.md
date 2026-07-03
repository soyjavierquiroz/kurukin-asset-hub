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

Editar las variables antes de desplegar. Como mínimo, cambiar `POSTGRES_PASSWORD` y alinear `DATABASE_URL` con ese valor.

```env
POSTGRES_PASSWORD=una-password-fuerte
DATABASE_URL=postgresql+psycopg://asset_hub:una-password-fuerte@db:5432/kurukin_asset_hub
ADMIN_USERNAME=admin
ADMIN_PASSWORD=otra-password-fuerte
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
docker run --rm --env-file .env --network kurukin-asset-hub_asset_hub_internal kurukin-asset-hub-web:admin-ui alembic upgrade head
```

O usar:

```bash
make migrate
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

## Admin UI

La UI interna vive en `/` y está renderizada con FastAPI, Jinja2, HTMX y Tailwind CDN. Permite revisar conteos del catálogo, filtrar assets, ver detalle técnico/editorial/transformación/seguridad y administrar sources, brands, products y niches con formularios server-rendered.

Las rutas web admin usan Basic Auth simple. Configurar estas variables en `.env` antes de publicar:

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=otra-password-fuerte
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
  kurukin-asset-hub-web:rclone-indexer \
  python scripts/index_rclone_source.py \
    --source-id drive_mujer_no_escribas \
    --remote gdrive_mne \
    --root "Assets Mujer No Escribas" \
    --brand mujer_no_escribas \
    --product metodo_pausa \
    --default-niche relaciones
```

Los assets se deduplican por `source + remote_path`; si un archivo ya existe para esa fuente, el indexador actualiza metadata como filename, extensión, tamaño, remote y fecha de indexado. El servicio web no monta `rclone.conf` por defecto y no falla si ese archivo no existe.

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
