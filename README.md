# Feeds de productos

Genera cada hora un feed completo para Google Merchant Center (y una versión para asistentes de IA)
de cada tienda listada en `tiendas.json`. Detecta solo la plataforma: Shopify, WooCommerce,
Tiendanube, Luna, VTEX, Magento, o cualquier otra con datos estructurados (JSON-LD).

## URLs (GitHub Pages)

- Resumen de todas las tiendas: `https://<usuario>.github.io/<repo>/`
- Feed para Google: `https://<usuario>.github.io/<repo>/feeds/<slug>.xml`
- Versión para IA: `https://<usuario>.github.io/<repo>/feeds/<slug>.json`

## Agregar una tienda (la forma fácil)

1. Pestaña **Actions** → **Agregar tienda** (columna izquierda) → botón **Run workflow**.
2. Pegá el link de la tienda (cualquier página sirve). Moneda, nombre y marca son opcionales.
3. Tocá **Run workflow**. En unos minutos, al entrar a la corrida, vas a ver el link del feed.

Con el mismo formulario podés **pausar**, **reactivar** o **eliminar** una tienda (campo *Qué hacer*).

## Ajustes avanzados

Para ajustes finos, editá el bloque de la tienda en `tiendas.json`. Solo `slug`, `nombre` y `url` son obligatorios:

| Campo | Para qué sirve |
|---|---|
| `slug` | Nombre corto del archivo del feed (sin espacios) |
| `nombre` | Nombre de la tienda |
| `url` | Página principal de la tienda |
| `moneda` | Moneda (ARS, CLP, COP, UYU, USD...). Obligatoria en VTEX; en las demás se detecta, pero conviene ponerla |
| `marca` | Marca para productos que no tengan una propia |
| `genero` | `male`, `female` o `unisex` si toda la tienda es de un género |
| `categoria_google` | Categoría de Google para toda la tienda (si no, se deduce de cada producto) |
| `categoria_google_defecto` | Categoría de Google solo para los productos que no se pudieron clasificar |
| `plataforma` | Forzar la plataforma (`shopify`, `woocommerce`, `tiendanube`, `luna`, `vtex`, `magento`, `generico`) |
| `quitar_parrafos` | Textos cuyos párrafos se eliminan de las descripciones (ej. políticas de devolución) |
| `ignorar_opciones` | Opciones de variante que no son variantes reales (ej. `puntos`) |
| `preorden_como` | `preorder` (por defecto) o `in_stock` para productos en pre-orden |
| `tiempo_max_min` | Minutos máximos por tienda en cada corrida (por defecto 8) |
| `feed_referencia` | Link del feed nativo de la tienda: en cada corrida se compara producto por producto (faltantes, precios, stock) |
| `activa` | `false` para pausar una tienda sin borrarla |
| `patron_productos` | Solo tiendas genéricas: expresión que identifica las URLs de producto en el sitemap |

## Control de calidad

Después de generar cada feed se revisa automáticamente:

- **Errores graves**: precio en cero o sin moneda, producto sin foto, IDs repetidos, oferta mayor al precio normal
  y cambios bruscos de precio respecto de la corrida anterior (más de 3 veces o menos de un tercio).
- **Avisos**: talle que parece color (o al revés), títulos en mayúsculas, productos sin descripción.
- **Comparación con el feed nativo** (si la tienda tiene `feed_referencia`): productos que faltan o sobran,
  precios y stock distintos.

El resultado aparece en el resumen de cada corrida, en la página principal y en `feeds/revision.json`.
Si aparece un error grave **nuevo**, la corrida termina en rojo y GitHub te avisa por mail (una sola vez por problema).
