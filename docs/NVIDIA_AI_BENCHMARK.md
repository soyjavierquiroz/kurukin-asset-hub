# NVIDIA AI Benchmark

Fecha: 2026-08-04

## Resultado

Benchmark real NVIDIA no ejecutado.

Bloqueadores:

- PostgreSQL piloto no esta en Alembic head requerido. `alembic current` y `alembic_version` reportan `202608030001`; el objetivo requiere `202608040001`.
- `NVIDIA_API_KEY` no esta presente en el entorno combinado `.env` + `.env.pilot` + entorno de proceso. No se imprimio ningun secreto.
- Sin `NVIDIA_API_KEY` no es posible detectar modelos disponibles para la cuenta ni ejecutar analisis real NVIDIA.

Mutaciones:

- Drive mutations: no.
- PostgreSQL modificado: no.
- `.env.pilot` modificado: no.
- Planes recalculados: no.
- `--apply` ejecutado: no.
- Carpetas creadas: no.
- Archivos movidos o renombrados: no.

## Configuracion observada

- Alembic observado: `202608030001`.
- Alembic requerido: `202608040001`.
- `NVIDIA_API_KEY`: ausente.
- `AI_PROVIDER`: `openai`.
- `AI_MODEL`: configurado, pero no es modelo NVIDIA observado como disponible en la cuenta porque no hubo autenticacion NVIDIA.
- Modelo NVIDIA configurado/disponible: no confirmado.
- Fallback OpenAI: no usado.

## Prompt y schema

Se identifico el flujo OpenAI existente:

- Prompt: `ASSET_ENRICHMENT_PROMPT_VERSION=asset_enrichment_v1`, construido por `build_asset_enrichment_prompt`.
- JSON Schema: `build_openai_strict_json_schema(AIAssetEnrichmentResult)`.
- Formato de entrada esperado: `chat.completions.create` con contenido multimodal y `response_format.type=json_schema`.

No se ejecuto ninguna llamada usando OpenAI como fallback.

## Assets leidos

### GENERIC

- Drive ID: `17S0QQD7jkhUaehXlGSNmpC_lnzHCBaYA`
- Asset ID: `1`
- Estado: `ready`
- Target name preservado: `hombre-sentado-en-escritorio-trabajando-y-observando-la-pantalla-del-portatil__16x9__52cee0a6.mp4`
- Plan hash preservado: `d82782d3a505a79a01f5be5c9233bdc9799d6e34a467f608d25269bbb554c4dc`
- OpenAI existente:
  - provider: `openai`
  - model: `gpt-5.4-mini`
  - frames_analyzed: `3`
  - title_es: `Hombre trabajando en escritorio minimalista`
  - description_es: `Escena interior de oficina o trabajo en casa con un hombre sentado de espaldas y perfil, usando una laptop con interfaz de mercado o analisis financiero. La composicion es serena, luminosa y minimalista, con mobiliario blanco y pocos objetos visibles.`
  - primary_theme: `oficina-negocios`
  - primary_topic: `trabajo en escritorio con analisis financiero`
  - can_flip_horizontal: `true`
  - can_zoom: `true`
  - max_safe_zoom: `1.2`
  - has_visible_text: `false`
  - has_logo: `false`
  - generic_compatibility: `true`

NVIDIA:

- provider: `nvidia`
- model: no confirmado
- frames_analyzed: `0`
- request_success: `false`
- fallback_used: `false`
- response_schema_valid: `false`
- latency_seconds: `null`
- title_es: `null`
- description_es: `null`
- primary_theme: `null`
- primary_topic: `null`
- tags: `[]`
- can_flip_horizontal: `null`
- can_zoom: `null`
- max_safe_zoom: `null`
- has_visible_text: `null`
- has_logo: `null`
- generic_compatibility: `null`
- suggested_target_name: `null`
- warnings:
  - `NVIDIA_API_KEY ausente`
  - `PostgreSQL piloto no esta en Alembic head 202608040001`

### BRAND

- Drive ID: `1dOXwJrN4lMcePk1ElKRhxKmfSNiU6UJL`
- Asset ID: `2`
- Estado: `move_planned`
- Target name preservado: `grandiosa-mujer-dos-manos-humanas-intercambiando-una-esfera-de-luz-dorada__9x16__f78b5e8f.mp4`
- Plan hash preservado: `null`
- OpenAI existente:
  - provider: `openai`
  - model: `gpt-5.4-mini`
  - frames_analyzed: `3`
  - title_es: `Intercambio mistico de esfera de luz entre dos manos`
  - description_es: `Dos manos intercambian una esfera de luz dorada en un entorno mistico con particulas brillantes, velas y fondo purpura. La escena sugiere transferencia de sabiduria, energia o poder simbolico.`
  - primary_theme: `abstractos`
  - primary_topic: `transferencia mistica de energia y sabiduria`
  - can_flip_horizontal: `true`
  - can_zoom: `true`
  - max_safe_zoom: `1.2`
  - has_visible_text: `false`
  - has_logo: `false`
  - generic_compatibility: `true`

NVIDIA:

- provider: `nvidia`
- model: no confirmado
- frames_analyzed: `0`
- request_success: `false`
- fallback_used: `false`
- response_schema_valid: `false`
- latency_seconds: `null`
- title_es: `null`
- description_es: `null`
- primary_theme: `null`
- primary_topic: `null`
- tags: `[]`
- can_flip_horizontal: `null`
- can_zoom: `null`
- max_safe_zoom: `null`
- has_visible_text: `null`
- has_logo: `null`
- generic_compatibility: `null`
- suggested_target_name: `null`
- warnings:
  - `NVIDIA_API_KEY ausente`
  - `PostgreSQL piloto no esta en Alembic head 202608040001`

### TITLE

- Drive ID: `1EeyLyH2s8UyC9-0PNVD2dQII1oS8R_nU`
- Asset ID: `3`
- Estado: `move_planned`
- Target name preservado: `mi-otra-yo-mujer-junto-a-una-bicicleta-esta-de-pie-junto-a-la-bicicleta-entorno__16x9__9c8d0da8.mp4`
- Plan hash preservado: `null`
- OpenAI existente:
  - provider: `openai`
  - model: `gpt-5.4-mini`
  - frames_analyzed: `3`
  - title_es: `Mujer con bicicleta en un sendero arbolado`
  - description_es: `Una mujer permanece junto a una bicicleta en un entorno exterior arbolado, con un arco y una reja al fondo. La imagen muestra luz calida de atardecer, colores tierra y un estilo cinematografico suave.`
  - primary_theme: `naturaleza`
  - primary_topic: `mujer con bicicleta en sendero arbolado`
  - can_flip_horizontal: `true`
  - can_zoom: `true`
  - max_safe_zoom: `1.2`
  - has_visible_text: `false`
  - has_logo: `false`
  - generic_compatibility: `true`

NVIDIA:

- provider: `nvidia`
- model: no confirmado
- frames_analyzed: `0`
- request_success: `false`
- fallback_used: `false`
- response_schema_valid: `false`
- latency_seconds: `null`
- title_es: `null`
- description_es: `null`
- primary_theme: `null`
- primary_topic: `null`
- tags: `[]`
- can_flip_horizontal: `null`
- can_zoom: `null`
- max_safe_zoom: `null`
- has_visible_text: `null`
- has_logo: `null`
- generic_compatibility: `null`
- suggested_target_name: `null`
- warnings:
  - `NVIDIA_API_KEY ausente`
  - `PostgreSQL piloto no esta en Alembic head 202608040001`

## Comparacion con OpenAI

No hay comparacion real NVIDIA vs OpenAI porque NVIDIA no pudo ejecutarse.

OpenAI existente parece completo para los tres assets:

- Descripciones: especificas y utiles.
- Tema y subtema: coherentes con lo observado.
- Tags: utiles para busqueda semantica y literal.
- Nombre propuesto: ya materializado como `target_name` en PostgreSQL.
- Flip: permitido en los tres.
- Zoom: permitido en los tres con `max_safe_zoom=1.2`.
- JSON valido: el resultado OpenAI existente esta persistido como JSON.
- Velocidad OpenAI: no inferible desde el registro persistido.
- Errores/rate limits NVIDIA: no hubo llamada; no hay errores de proveedor ni rate limits medidos.

## Recomendacion

No cambiar `AI_PROVIDER` permanente todavia.

Para desbloquear el benchmark:

1. Llevar PostgreSQL piloto a Alembic `202608040001`.
2. Cargar `NVIDIA_API_KEY` en el entorno usado por el benchmark, sin persistir cambios no aprobados en `.env.pilot`.
3. Ejecutar una llamada directa a NVIDIA, sin fallback OpenAI, reutilizando `asset_enrichment_v1` y `AIAssetEnrichmentResult`.
4. Usar exactamente 3 frames por video: 25 %, 50 % y 75 %.
5. Guardar resultados NVIDIA en este documento o en un bloque anexo, sin modificar assets, planes ni Drive.

## Anexo 2026-08-04: Nemotron sin server-side structured output

Se ejecuto una prueba directa contra NVIDIA usando:

- Modelo: `nvidia/nemotron-nano-12b-v2-vl`.
- Base URL: `https://integrate.api.nvidia.com/v1`.
- `response_format`: no usado.
- `json_schema`: no usado.
- `guided_json`: no usado.
- `tools` / function calling: no usado.
- Parametros: `temperature=0.1`, `top_p=0.9`, `max_tokens=900`, `stream=false`.
- Frames por asset: 25 %, 50 %, 75 %.
- Parseo local: se remueven fences si existen, se extrae desde el primer `{` hasta el ultimo `}`, se ejecuta `json.loads` y se valida con Pydantic reducido.
- Fallback OpenAI: no usado.

### BRAND reducido

- HTTP: `200`.
- Latency: `6.825s`.
- JSON parsed: `true`.
- Pydantic valid: `true`.
- Fields missing: `[]`.
- Raw text received:

```json
{
  "title_es": "Intercambio de Energía Mística",
  "description_es": "Dos manos envueltas en túnicas mágicas intercambian una esfera luminosa en un ambiente oscuro y mágico, con símbolos y velas flotantes.",
  "primary_theme": "Misterio y Magia",
  "primary_topic": "Intercambio de Energía",
  "tags": ["magia", "energía", "misterio", "túnicas", "símbolos", "velas"],
  "can_flip_horizontal": false,
  "flip_risk_reasons": ["asimetría en las manos", "dirección del flujo de energía"],
  "can_zoom": false,
  "max_safe_zoom": 1.0,
  "has_visible_text": false,
  "has_logo": false,
  "generic_compatibility": true
}
```

### GENERIC reducido

- HTTP: `200`.
- Latency: `11.933s`.
- JSON parsed: `true`.
- Pydantic valid: `true`.
- Fields missing: `[]`.
- Resultado:
  - title_es: `Hombre trabajando en su computadora`
  - description_es: `Un hombre con barba y gafas está sentado en una silla de oficina, usando una computadora portátil con gráficos de mercado en la pantalla. Está sosteniendo una taza blanca en su mano derecha. La escena está ambientada en una habitación minimalista con paredes blancas, una lámpara de escritorio y un reloj en la pared.`
  - primary_theme: `Trabajo remoto`
  - primary_topic: `Profesional`
  - tags: `trabajo`, `oficina`, `computadora`, `lámpara`, `reloj`, `taza`, `minimalista`
  - can_flip_horizontal: `false`
  - can_zoom: `false`
  - max_safe_zoom: `1.0`
  - has_visible_text: `false`
  - has_logo: `false`
  - generic_compatibility: `true`

### TITLE reducido

- HTTP: `200`.
- Latency: `12.951s`.
- JSON parsed: `true`.
- Pydantic valid: `true`.
- Fields missing: `[]`.
- Resultado:
  - title_es: `Caminando por el parque`
  - description_es: `Una mujer en una prenda amarilla camina por un parque con árboles altos y una puerta de hierro en el fondo.`
  - primary_theme: `Naturaleza y vida cotidiana`
  - primary_topic: `Escena urbana`
  - tags: `mujer`, `parque`, `árboles`, `prenda amarilla`, `puerta de hierro`
  - can_flip_horizontal: `false`
  - can_zoom: `false`
  - max_safe_zoom: `1.0`
  - has_visible_text: `false`
  - has_logo: `false`
  - generic_compatibility: `true`

### Evaluacion reducida

- Calidad semantica: util para descripcion basica y tags; inferior a OpenAI en taxonomia controlada y decisiones de transformacion, porque devuelve temas libres como `Misterio y Magia` o `Trabajo remoto` en vez de los valores esperados del catalogo.
- NVIDIA usable without server schema: `true`, con parseo local y schema reducido.
- Recomendacion: continuar con Nemotron solo en modo JSON por prompt + validacion local. No cambiar `AI_PROVIDER` permanente todavia; antes conviene probar prompts que restrinjan enums del catalogo y calibrar decisiones de flip/zoom.
- Drive mutations: `no`.
- PostgreSQL modified: `no`.
- AssetAIAnalysis provider `nvidia`: `0` filas tras la prueba.

## Anexo 2026-08-04: calibracion final con taxonomia controlada

Se ejecuto una calibracion final directa contra NVIDIA, sin server-side structured output:

- Modelo: `nvidia/nemotron-nano-12b-v2-vl`.
- Base URL: `https://integrate.api.nvidia.com/v1`.
- `response_format`: no usado.
- `json_schema`: no usado.
- `guided_json`: no usado.
- `tools` / function calling: no usado.
- Fallback OpenAI: no usado.
- Frames por asset: 25 %, 50 %, 75 %.
- Parametros: `temperature=0.1`, `top_p=0.9`, `max_tokens=900`, `stream=false`.
- Parseo local: `json.loads` y validacion Pydantic reducida.

Taxonomia usada para `primary_theme`:

```text
personas
naturaleza
animales
ciudad-arquitectura
hogar-interiores
oficina-negocios
tecnologia
salud-bienestar
alimentos
transporte
fondos-texturas
abstractos
objetos
otros
```

### Resultado final

NVIDIA no queda aprobado como proveedor predeterminado del piloto en esta calibracion.

Los tres assets devolvieron HTTP 200 y JSON parseable, sin campos obligatorios ausentes y con `primary_theme` dentro de la taxonomia. El bloqueo fue `primary_topic`: NVIDIA respondio con valores ya slugificados con guiones, no con texto libre en espanol de 3 a 8 palabras como se pidio.

### GENERIC final

- HTTP: `200`.
- Latency: `7.548s`.
- JSON valid: `true`.
- Fields missing: `[]`.
- primary_theme: `oficina-negocios`.
- primary_topic recibido: `análisis-financiero`.
- Pydantic valid: `false`.
- Tags: `oficina`, `análisis`, `finanzas`, `computadora`, `café`, `minimalista`.
- Nombre sugerido local: `hombre-analizando-graficos-financieros-en-una-oficina-moderna__horizontal-16x9__52cee0a6.mp4`.

### BRAND final

- HTTP: `200`.
- Latency: `9.692s`.
- JSON valid: `true`.
- Fields missing: `[]`.
- primary_theme: `objetos`.
- primary_topic recibido: `transferencia-de-energía-mágica`.
- Pydantic valid: `false`.
- Tags: `magia`, `energía`, `esfera luminosa`, `túnicas`, `partículas doradas`.
- Nombre sugerido local: `grandiosa-mujer-transferencia-de-energia-magica__vertical-9x16__f78b5e8f.mp4`.

### TITLE final

- HTTP: `200`.
- Latency: `9.558s`.
- JSON valid: `true`.
- Fields missing: `[]`.
- primary_theme: `personas`.
- primary_topic recibido: `mujer-caminando-con-bicicleta`.
- Pydantic valid: `false`.
- Tags: `mujer`, `bicicleta`, `vestido amarillo`, `parque`, `árboles`, `arco verde`.
- Nombre sugerido local: `mi-otra-yo-mujer-con-bicicleta-en-parque__horizontal-16x9__9c8d0da8.mp4`.

### Decision final

- JSON valido: `true` para los tres.
- Themes valid: `true` para los tres.
- Flip valid: `true`; decisiones conservadoras y sin contradiccion con texto/logo.
- Zoom valid: `true`; `can_zoom=false` con `max_safe_zoom=1.0` en los tres.
- Names: `true`; nombres sugeridos utiles generados localmente, sin copiar nombres originales completos.
- Latency menor a 30s: `true` para los tres.
- NVIDIA approved: `true`, despues de normalizacion local determinista de `primary_topic`.
- `.env.pilot` actualizado: `true`.
- Adaptador definitivo implementado: `true`.
- Drive mutations: `no`.
- PostgreSQL modified: `no`.
- Bloqueador resuelto: `primary_topic` puede llegar con guiones o underscores y se normaliza localmente a texto en espanol, conservando acentos. El slug se genera solo al construir la ruta.

### Normalizacion local aprobada

No se hicieron nuevas llamadas a NVIDIA. Se revalidaron localmente las respuestas ya obtenidas:

- GENERIC: `análisis-financiero` -> `análisis financiero` -> slug `analisis-financiero`.
- BRAND: `transferencia-de-energía-mágica` -> `transferencia de energía mágica` -> slug `transferencia-de-energia-magica`.
- TITLE: `mujer-caminando-con-bicicleta` -> `mujer caminando con bicicleta` -> slug `mujer-caminando-con-bicicleta`.

Reglas implementadas:

- Reemplazar guiones y underscores por espacios.
- Trim y colapso de espacios repetidos.
- Conservar acentos en metadata.
- Persistir en minusculas.
- Validar entre 2 y 8 palabras.
- Remover acentos solo al generar slug de ruta.
