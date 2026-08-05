# Asset Hub Operations

## A. Comprobar sistema

```bash
asset-hub drive doctor --env-file /opt/apps/kurukin-asset-hub/.env.pilot
```

## B. Analizar hasta 50 assets

```bash
asset-hub drive batch --env-file /opt/apps/kurukin-asset-hub/.env.pilot --max-total 50 --max-per-scope 25
```

## C. Revisar planes

```bash
asset-hub drive status --env-file /opt/apps/kurukin-asset-hub/.env.pilot --status move_planned
```

## D. Mover planes

```bash
asset-hub drive batch --env-file /opt/apps/kurukin-asset-hub/.env.pilot --apply --max-total 50 --max-per-scope 25
```

## E. Revisar casos dudosos

```bash
asset-hub drive review --env-file /opt/apps/kurukin-asset-hub/.env.pilot list
```

```bash
asset-hub drive review --env-file /opt/apps/kurukin-asset-hub/.env.pilot approve --file-id ID --primary-theme personas --primary-topic "bienestar yoga" --tags "persona,yoga,bienestar" --yes
```

## F. Comprobar resultados

```bash
asset-hub drive status --env-file /opt/apps/kurukin-asset-hub/.env.pilot --status ready
```

## G. Migrar layout antiguo

```bash
asset-hub drive layout --env-file /opt/apps/kurukin-asset-hub/.env.pilot migrate --from legacy --to compact_v2
```
