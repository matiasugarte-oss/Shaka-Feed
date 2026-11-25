"""
Agrega, pausa, reactiva o elimina una tienda de tiendas.json. Lo usa el workflow "Agregar tienda".

Datos (variables de entorno que completa el formulario de GitHub Actions):
  ACCION       agregar | pausar | reactivar | eliminar
  URL_TIENDA   link de la tienda (cualquier página sirve: se usa el dominio)
  NOMBRE       opcional
  MONEDA       opcional (si no se indica, se deduce del dominio: .uy -> UYU, .cl -> CLP...)
  MARCA        opcional

Uso para el resumen:  python agregar_tienda.py --resumen <slug>
"""
import json
import os
import re
import shutil
import sys
from urllib.parse import urlparse

CONFIG = "tiendas.json"
MONEDA_POR_DOMINIO = {"uy": "UYU", "ar": "ARS", "cl": "CLP", "co": "COP", "mx": "MXN", "pe": "PEN",
                      "br": "BRL", "py": "PYG", "bo": "BOB", "ec": "USD", "es": "EUR", "us": "USD"}


def host_de(url):
    return urlparse(url).netloc.lower().replace("www.", "")


def salida(clave, valor):
    """Pasa un dato al siguiente paso del workflow."""
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.getenv("GITHUB_OUTPUT"), "a", encoding="utf-8") as f:
            f.write(f"{clave}={valor}\n")


def resumen(slug):
    """Escribe en la página de la corrida el resultado y el link del feed."""
    try:
        estado = json.load(open("feeds/estado.json", encoding="utf-8")).get(slug, {})
    except (OSError, ValueError):
        estado = {}
    dueno, _, repo = (os.getenv("GITHUB_REPOSITORY") or "usuario/repo").partition("/")
    base = f"https://{dueno.lower()}.github.io/{repo}"
    if estado.get("error") or not estado.get("productos"):
        texto = (f"## ⚠️ {estado.get('nombre', slug)}\n\nNo se pudo generar el feed: "
                 f"{estado.get('error') or 'no se encontraron productos'}.\n\n"
                 "Revisá el registro de la corrida (paso *Generar el feed*) para ver el detalle.\n")
    else:
        texto = (f"## ✅ {estado['nombre']}\n\n"
                 f"- Plataforma detectada: **{estado.get('plataforma')}**\n"
                 f"- Productos: **{estado.get('productos')}** ({estado.get('items')} ítems con variantes)\n\n"
                 f"**Feed para Google Merchant Center:**\n`{base}/feeds/{slug}.xml`\n\n"
                 f"**Versión para el asistente de IA:**\n`{base}/feeds/{slug}.json`\n\n"
                 f"El link funciona en uno o dos minutos, cuando termine de publicar GitHub Pages. "
                 f"Desde ahora se actualiza solo cada hora. Resumen de todas las tiendas: {base}/\n")
    try:
        rev = json.load(open("feeds/revision.json", encoding="utf-8")).get(slug, {})
    except (OSError, ValueError):
        rev = {}
    if rev:
        texto += "\n### Revisión automática\n\n"
        if not rev.get("graves") and not rev.get("avisos"):
            texto += "✅ Sin problemas detectados.\n"
        for tipo, icono in (("graves", "❌"), ("avisos", "⚠️")):
            for regla, info in rev.get(tipo, {}).items():
                texto += f"- {icono} **{regla}** ({info['cantidad']}): " + "; ".join(info["ejemplos"][:3]) + "\n"
        comp = rev.get("comparacion") or {}
        if comp.get("totales"):
            tot = comp["totales"]
            texto += (f"- 🔍 **Contra el feed nativo**: {comp['productos_nuestro']} productos nuestros vs "
                      f"{comp['productos_nativo']} nativos · faltan {tot['faltan']} · sobran {tot['sobran']} · "
                      f"precio distinto {tot['precio_distinto']} · stock distinto {tot['stock_distinto']}\n")
    print(texto)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a", encoding="utf-8") as f:
            f.write(texto)


def main():
    accion = (os.getenv("ACCION") or "agregar").strip().lower()
    url = (os.getenv("URL_TIENDA") or "").strip()
    if not url:
        sys.exit("Falta el link de la tienda.")
    if not url.startswith("http"):
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc or "." not in p.netloc:
        sys.exit(f"El link no parece válido: {url}")
    base, host = f"{p.scheme}://{p.netloc.lower()}", host_de(url)

    tiendas = json.load(open(CONFIG, encoding="utf-8"))
    tienda = next((t for t in tiendas if host_de(t["url"]) == host), None)

    if accion == "eliminar":
        if not tienda:
            sys.exit(f"No hay ninguna tienda con el dominio {host}.")
        tiendas.remove(tienda)
        for ruta in (f"feeds/{tienda['slug']}.xml", f"feeds/{tienda['slug']}.json", f"datos/{tienda['slug']}.json"):
            if os.path.exists(ruta):
                os.remove(ruta)
        try:
            estado = json.load(open("feeds/estado.json", encoding="utf-8"))
            estado.pop(tienda["slug"], None)
            json.dump(estado, open("feeds/estado.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except (OSError, ValueError):
            pass
        print(f"Tienda eliminada: {tienda.get('nombre')} ({tienda['slug']})")
    elif accion in ("pausar", "reactivar"):
        if not tienda:
            sys.exit(f"No hay ninguna tienda con el dominio {host}.")
        tienda["activa"] = accion == "reactivar"
        print(f"Tienda {'reactivada' if tienda['activa'] else 'pausada'}: {tienda.get('nombre')}")
    else:
        nueva = tienda is None
        if nueva:
            slug = re.sub(r"[^a-z0-9]+", "-", host.split(".")[0]).strip("-") or "tienda"
            existentes, n = {t["slug"] for t in tiendas}, 2
            while slug in existentes:
                slug, n = f"{slug.rstrip('0123456789-')}-{n}", n + 1
            tienda = {"slug": slug, "url": base}
            tiendas.append(tienda)
        for campo, var in (("nombre", "NOMBRE"), ("moneda", "MONEDA"), ("marca", "MARCA"), ("feed_referencia", "FEED_NATIVO")):
            valor = (os.getenv(var) or "").strip()
            if valor:
                tienda[campo] = valor.upper() if campo == "moneda" else valor
        tienda.setdefault("nombre", host.split(".")[0].replace("-", " ").title())
        if "moneda" not in tienda:
            tld = host.rsplit(".", 1)[-1]
            if tld in MONEDA_POR_DOMINIO:
                tienda["moneda"] = MONEDA_POR_DOMINIO[tld]
        tienda["activa"] = True
        print(f"Tienda {'agregada' if nueva else 'actualizada'}: {json.dumps(tienda, ensure_ascii=False)}")

    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(tiendas, f, ensure_ascii=False, indent=2)
        f.write("\n")
    salida("slug", (tienda or {}).get("slug", ""))
    salida("generar", "si" if accion in ("agregar", "reactivar") else "no")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--resumen":
        resumen(sys.argv[2])
    else:
        main()
