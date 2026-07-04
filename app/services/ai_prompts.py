from __future__ import annotations

from app.config import get_settings
from app.models import Asset

ASSET_ENRICHMENT_PROMPT_VERSION = "asset_enrichment_v1"


def build_asset_enrichment_prompt(asset: Asset) -> str:
    output_language = get_settings().ai_output_language or "es"
    return f"""
Analiza este asset como parte de una biblioteca para creacion automatica de videos.
Devuelve solo JSON valido compatible con el schema solicitado.

Objetivo:
- Identifica contenido visual, acciones, objetos, estilo, mood, colores, uso narrativo y rol de escena.
- Detecta texto visible, logos y watermarks.
- Determina si es seguro hacer flip horizontal y otras transformaciones.
- Determina si es seguro para subtitulos y overlays de texto.
- Genera keywords estructuradas con category, weight, confidence y language si aplica.
- Genera best_for, avoid_for, search_text y embedding_text utiles para busqueda semantica.
- No inventes marca, producto, ubicacion, cultura o personas si no hay evidencia visual.
- No decidas permisos de marca, producto, usage_scope ni rights_status; eso lo hacen las policies.
- Si hay duda, baja confidence y marca needs_human_review con review_reason claro.

Idioma de salida:
- output_language: {output_language}
- Responde en español neutro.
- Todos los campos de texto libre deben estar en español.
- Todas las keywords deben estar en español, salvo nombres propios o términos de marca.
- No mezcles inglés y español.
- Mantén los valores enum exactamente como se definen en el schema, aunque estén en inglés.
- Los campos search_text y embedding_text deben estar en español, con sinónimos útiles para búsqueda.
- No traduzcas nombres propios como Veyra o Grandiosa Mujer.
- Existing analyses no se migran automaticamente; para regenerar en español hay que correr --force.

Contexto conocido del catalogo:
- asset_id: {asset.id}
- asset_uid: {asset.asset_uid}
- filename: {asset.filename}
- type: {asset.type}
- mime_type: {asset.mime_type or "unknown"}
- orientation_actual: {asset.orientation}
- duration_seconds: {asset.duration_seconds if asset.duration_seconds is not None else "unknown"}
- usage_scope_actual_no_modificar: {asset.usage_scope}
- rights_status_actual_no_modificar: {asset.rights_status}

Valores permitidos:
- shot_type: closeup, medium, wide, detail, establishing, unknown
- camera_motion: static, handheld, pan, tilt, zoom, tracking, drone, unknown
- subject_position: center, left, right, top, bottom, full_frame, unknown
- visual_energy: low, medium, high, unknown
- pacing: slow, medium, fast, unknown
- best_scene_role: hook, intro, explanation, emotional_peak, transition, broll, outro, background, unknown
- overlay_safe_area: top, center, bottom, left, right, full, unknown
- keyword.category: subject, object, action, location, mood, style, color, concept, culture, scene_role, usage, restriction, other

Reglas:
- search_text debe ser texto compacto para busqueda literal.
- embedding_text debe ser una descripcion semantica natural.
- best_for y avoid_for deben describir usos creativos, no permisos legales.
- needs_human_review debe ser true si confidence es baja, si hay texto/logo/watermark ambiguo, o si hay riesgo de interpretacion.
""".strip()
