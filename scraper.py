"""
Scraper de productos para tiendas Tiendanube (pensado para https://sh-aka.com.ar).

Estrategia:
  1. Obtiene las URLs de productos desde el sitemap (con fallback al listado /productos/).
  2. En cada página de producto lee:
     - el JSON-LD (schema.org Product): nombre, descripción, imágenes, marca
     - el atributo data-variants del formulario: precio, precio tachado, stock y talle por variante
  3. Escribe feed.json (un objeto por producto) y feed.csv (una fila por variante).

Uso:  pip install requests beautifulsoup4 lxml
      python scraper.py
"""
import csv
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://sh-aka.com.ar"
DELAY = 0.8          # segundos entre requests (ser amable con el servidor)
TIMEOUT = 20
MAX_LIST_PAGES = 50  # tope de seguridad para el fallback de listado

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ProductFeedBot/1.0)",
    "Accept-Language": "es-AR,es;q=0.9",
})


def get(url, retries=3):
    for intento in range(retries):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
        except requests.RequestException as e:
            print(f"  error {e} en {url}", file=sys.stderr)
        time.sleep(2 * (intento + 1))
    return None


def es_producto(url):
    path = urlparse(url).path
    return "/productos/" in path and path.rstrip("/") != "/productos"


# ---------- 1. Descubrir URLs de productos ----------

def urls_desde_sitemap():
    urls, pendientes, vistos = set(), [f"{BASE}/sitemap.xml"], set()
    while pendientes:
        sm = pendientes.pop()
        if sm in vistos:
            continue
        vistos.add(sm)
        r = get(sm)
        if not r:
            continue
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text)
        for loc in locs:
            loc = html.unescape(loc)
            if loc.endswith(".xml"):
                pendientes.append(loc)          # sitemap índice
            elif es_producto(loc):
                urls.add(loc.split("?")[0])
    return urls


def urls_desde_listado():
    urls = set()
    for page in range(1, MAX_LIST_PAGES + 1):
        r = get(f"{BASE}/productos/?page={page}")
        if not r:
            break
        soup = BeautifulSoup(r.text, "lxml")
        nuevos = {
            urljoin(BASE, a["href"]).split("?")[0]
            for a in soup.select("a[href]")
            if es_producto(urljoin(BASE, a["href"]))
        } - urls
        if not nuevos:
            break
        urls |= nuevos
        time.sleep(DELAY)
    return urls


# ---------- 2. Parsear cada producto ----------

def leer_json_ld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for it in items:
            if isinstance(it, dict) and it.get("@type") in ("Product", ["Product"]):
                return it
    return {}


def leer_variantes(soup):
    nodo = soup.find(attrs={"data-variants": True})
    if not nodo:
        return []
    try:
        return json.loads(html.unescape(nodo["data-variants"]))
    except json.JSONDecodeError:
        return []


def parsear_producto(url):
    r = get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    ld = leer_json_ld(soup)
    variantes = leer_variantes(soup)

    nombre = ld.get("name") or (soup.find("h1").get_text(strip=True) if soup.find("h1") else "")
    imagenes = ld.get("image") or []
    if isinstance(imagenes, str):
        imagenes = [imagenes]
    marca = ld.get("brand", {})
    marca = marca.get("name") if isinstance(marca, dict) else marca

    pid = None
    nodo_id = soup.find(attrs={"data-product-id": True})
    if nodo_id:
        pid = nodo_id["data-product-id"]

    vars_limpias = []
    for v in variantes:
        opciones = [v.get(f"option{i}") for i in range(3) if v.get(f"option{i}")]
        vars_limpias.append({
            "variant_id": v.get("id"),
            "sku": v.get("sku"),
            "opciones": " / ".join(map(str, opciones)),
            "precio": v.get("price_number"),
            "precio_tachado": v.get("compare_at_price_number"),
            "stock": v.get("stock"),            # None suele significar stock ilimitado
            "disponible": v.get("available"),
            "imagen": v.get("image_url"),
        })

    # Si no hubo data-variants, usamos el precio del JSON-LD
    if not vars_limpias:
        offers = ld.get("offers", {})
        offers = offers[0] if isinstance(offers, list) and offers else offers
        vars_limpias.append({
            "variant_id": None, "sku": ld.get("sku"), "opciones": "",
            "precio": offers.get("price") if isinstance(offers, dict) else None,
            "precio_tachado": None, "stock": None,
            "disponible": "InStock" in str(offers.get("availability", "")) if isinstance(offers, dict) else None,
            "imagen": None,
        })

    return {
        "product_id": pid,
        "url": url,
        "nombre": nombre,
        "marca": marca,
        "descripcion": (ld.get("description") or "").strip(),
        "imagenes": imagenes,
        "variantes": vars_limpias,
    }


# ---------- 3. Feed XML para Google Merchant Center ----------

MARCA = "Shaka Cmmnty"
GENERO = "male"        # male / female / unisex: ajustalo según el público de la marca
EDAD = "adult"
MONEDA = "ARS"

# (palabras clave en el nombre, tipo singular para el título, product_type, categoría de Google)
CATEGORIAS = [
    (["BOARDSHORT", "SWIM"], "Traje de baño", "Trajes de baño",
     "Apparel & Accessories > Clothing > Swimwear"),
    (["JORT", "SHORT", "BERMUDA"], "Bermuda", "Bermudas",
     "Apparel & Accessories > Clothing > Shorts"),
    (["BOXER"], "Boxer", "Boxers",
     "Apparel & Accessories > Clothing > Underwear & Socks"),
    (["SOCKS"], "Medias", "Medias",
     "Apparel & Accessories > Clothing > Underwear & Socks"),
    (["CAP", "KEPI", "HAT", "BEANIE", "GORRO"], "Gorra", "Headwear",
     "Apparel & Accessories > Clothing Accessories > Hats"),
    (["BELT"], "Cinturón", "Accesorios",
     "Apparel & Accessories > Clothing Accessories > Belts"),
    (["JACKET", "WINDBREAKER", "VEST", "PUFFER", "PARKA"], "Campera", "Camperas",
     "Apparel & Accessories > Clothing > Outerwear > Coats & Jackets"),
    (["DENIM", "PANT", "JEAN"], "Pantalón", "Pantalones",
     "Apparel & Accessories > Clothing > Pants"),
    (["KNIT", "SWEATER"], "Sweater", "Sweaters",
     "Apparel & Accessories > Clothing > Shirts & Tops"),
    (["HOOD", "HOODIE", "ZIP", "CREW"], "Buzo", "Buzos",
     "Apparel & Accessories > Clothing > Shirts & Tops"),
    (["POLO"], "Chomba", "Polos",
     "Apparel & Accessories > Clothing > Shirts & Tops"),
    (["TANK"], "Musculosa", "Musculosas",
     "Apparel & Accessories > Clothing > Shirts & Tops"),
    (["SHIRT"], "Camisa", "Camisas",
     "Apparel & Accessories > Clothing > Shirts & Tops"),
    (["TEE", "LONGSLEEVE"], "Remera", "Remeras",
     "Apparel & Accessories > Clothing > Shirts & Tops"),
]

COLORES = {"negro", "black", "blanco", "white", "azul", "blue", "marino", "celeste", "rojo",
           "red", "verde", "green", "gris", "grey", "gray", "melange", "topo", "rosa", "pink",
           "beige", "crudo", "vison", "marron", "brown", "tierra", "amarillo", "yellow",
           "naranja", "violeta", "bordo", "stone", "carbon", "asphalt", "oscuro", "claro"}


def clasificar(nombre):
    palabras = set(re.findall(r"[A-ZÁÉÍÓÚÜÑ]+", nombre.upper()))
    for claves, singular, tipo, cat in CATEGORIAS:
        if palabras & set(claves):
            return singular, tipo, cat
    return "Prenda", "Otros", "Apparel & Accessories > Clothing"


def limpiar_nombre(nombre):
    """Saca el texto de preventa y pasa el nombre de MAYÚSCULAS a formato título."""
    base = re.sub(r"\s*-?\s*PRE-?VENTA.*$", "", nombre, flags=re.I).strip(" -")

    def fmt(palabra):
        if palabra.upper() in ("SHK", "<3"):
            return palabra.upper()
        return palabra.capitalize() if palabra.isupper() else palabra
    return " ".join(fmt(p) for p in base.split())


def extraer_color(nombre):
    m = re.search(r"\(([^)]+)\)\s*$", nombre)
    if m and set(m.group(1).lower().split()) & COLORES:
        return m.group(1).replace("<3", "").strip().capitalize()
    return None


def fecha_preventa(nombre):
    m = re.search(r"a partir del?\s*(\d{1,2})/(\d{1,2})", nombre, flags=re.I)
    if not m:
        return None
    hoy = datetime.now()
    dia, mes = int(m.group(1)), int(m.group(2))
    anio = hoy.year + (1 if (mes, dia) < (hoy.month, hoy.day) and hoy.month - mes > 6 else 0)
    try:
        return datetime(anio, mes, dia).strftime("%Y-%m-%dT00:00-03:00")
    except ValueError:
        return None


def url_absoluta(u):
    if not u:
        return None
    return "https:" + u if u.startswith("//") else u


def precio_fmt(n):
    return f"{float(n):.2f} {MONEDA}"


def generar_google_xml(productos, ruta):
    from xml.sax.saxutils import escape

    items = []
    for p in productos:
        nombre_orig = p["nombre"] or ""
        if not nombre_orig or "GIFT" in nombre_orig.upper():
            continue  # las gift cards no se pueden publicitar en Shopping
        nombre = limpiar_nombre(nombre_orig)
        singular, tipo, categoria = clasificar(nombre_orig)
        color = extraer_color(nombre)
        preventa = bool(re.search(r"PRE-?VENTA", nombre_orig, flags=re.I))
        fecha_disp = fecha_preventa(nombre_orig) if preventa else None

        desc = BeautifulSoup(p["descripcion"] or "", "lxml").get_text(" ", strip=True)
        if not desc:
            desc = f"{nombre}. {singular} de {MARCA}."
        imagenes = [url_absoluta(i) for i in p["imagenes"] if i]

        for v in p["variantes"]:
            precio_actual = v.get("precio")
            if not precio_actual:
                continue
            tachado = v.get("precio_tachado")
            talle = (v.get("opciones") or "").split(" / ")[0].strip()
            if talle.upper() in ("TALLE UNICO", "TALLE ÚNICO", "UNICO", "ÚNICO"):
                talle = "Único"

            titulo = f"{nombre} | {singular} {MARCA}"
            if talle and talle != "Único":
                titulo += f" - Talle {talle}"

            if preventa:
                disponibilidad = "preorder"
            elif v.get("disponible") is False or v.get("stock") == 0:
                disponibilidad = "out_of_stock"
            else:
                disponibilidad = "in_stock"

            imagen = url_absoluta(v.get("imagen")) or (imagenes[0] if imagenes else None)
            if not imagen:
                continue  # Google rechaza productos sin imagen
            extras = [i for i in imagenes if i != imagen][:10]

            campos = [
                ("g:id", v.get("variant_id") or f"{p['product_id']}-{talle}"),
                ("g:item_group_id", p["product_id"]),
                ("g:title", titulo[:150]),
                ("g:description", desc[:5000]),
                ("g:link", p["url"]),
                ("g:image_link", imagen),
                *[("g:additional_image_link", i) for i in extras],
                ("g:availability", disponibilidad),
                ("g:availability_date", fecha_disp),
                ("g:condition", "new"),
                ("g:brand", MARCA),
                ("g:google_product_category", categoria),
                ("g:product_type", f"{tipo} > {nombre}"),
                ("g:gender", GENERO),
                ("g:age_group", EDAD),
                ("g:size", talle or None),
                ("g:color", color),
            ]
            if tachado and float(tachado) > float(precio_actual):
                campos += [("g:price", precio_fmt(tachado)),
                           ("g:sale_price", precio_fmt(precio_actual))]
            else:
                campos.append(("g:price", precio_fmt(precio_actual)))
            if v.get("sku"):
                campos.append(("g:mpn", v["sku"]))
            else:
                campos.append(("g:identifier_exists", "no"))

            lineas = [f"      <{k}>{escape(str(val))}</{k}>" for k, val in campos if val]
            items.append("    <item>\n" + "\n".join(lineas) + "\n    </item>")

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">\n'
        "  <channel>\n"
        f"    <title>{escape(MARCA)}</title>\n"
        f"    <link>{BASE}</link>\n"
        f"    <description>Feed de productos de {escape(MARCA)}</description>\n"
        + "\n".join(items) +
        "\n  </channel>\n</rss>\n"
    )
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(xml)
    print(f"feed.xml: {len(items)} items")


# ---------- 4. Escribir los feeds ----------

def main():
    inicio = time.time()
    urls = urls_desde_sitemap()
    print(f"Sitemap: {len(urls)} productos")
    if not urls:
        urls = urls_desde_listado()
        print(f"Listado (fallback): {len(urls)} productos")

    productos = []
    for i, url in enumerate(sorted(urls), 1):
        p = parsear_producto(url)
        if p:
            productos.append(p)
        print(f"[{i}/{len(urls)}] {url}")
        time.sleep(DELAY)

    generado = datetime.now(timezone.utc).isoformat()
    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump({"generado": generado, "total": len(productos), "productos": productos},
                  f, ensure_ascii=False, indent=2)

    campos = ["product_id", "variant_id", "sku", "nombre", "opciones", "precio",
              "precio_tachado", "stock", "disponible", "url", "imagen"]
    with open("feed.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        for p in productos:
            for v in p["variantes"]:
                w.writerow({
                    "product_id": p["product_id"], "nombre": p["nombre"], "url": p["url"],
                    **{k: v[k] for k in ("variant_id", "sku", "opciones", "precio",
                                         "precio_tachado", "stock", "disponible")},
                    "imagen": v["imagen"] or (p["imagenes"][0] if p["imagenes"] else ""),
                })

    generar_google_xml(productos, "feed.xml")

    print(f"Listo: {len(productos)} productos en {time.time() - inicio:.0f}s")


if __name__ == "__main__":
    main()
