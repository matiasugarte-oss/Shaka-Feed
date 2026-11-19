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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://sh-aka.com.ar"
DELAY = 0.5          # pausa de cada worker entre requests (ser amable con el servidor)
WORKERS = 4          # requests en paralelo
TIMEOUT = 15
TIEMPO_MAX = 10 * 60  # corta a los 10 min y guarda lo que tenga, para no pasarse del límite de GitHub
MAX_LIST_PAGES = 50  # tope de seguridad para el fallback de listado

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ProductFeedBot/1.0)",
    "Accept-Language": "es-AR,es;q=0.9",
})


ESTADOS = {}


def get(url, retries=2):
    for intento in range(retries):
        try:
            r = session.get(url, timeout=TIMEOUT)
            ESTADOS[r.status_code] = ESTADOS.get(r.status_code, 0) + 1
            if r.status_code == 200:
                return r
            if r.status_code in (403, 404, 429):
                if r.status_code != 404:
                    print(f"  HTTP {r.status_code} en {url}", flush=True)
                return None
        except requests.RequestException as e:
            ESTADOS["error"] = ESTADOS.get("error", 0) + 1
            print(f"  error {type(e).__name__} en {url}", flush=True)
        time.sleep(2 * (intento + 1))
    return None


def es_producto(url):
    """Solo productos de la tienda principal (Argentina), sin las versiones /cl/, /cr/, etc."""
    u = urlparse(url)
    if u.netloc and u.netloc.replace("www.", "") != urlparse(BASE).netloc:
        return False
    partes = [x for x in u.path.split("/") if x]
    return len(partes) == 2 and partes[0] == "productos"


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

SIZE_RE = re.compile(r"^(XXS|XS|S|M|L|XL|XXL|XXXL|\d{1,3}|TALLE\s+(UNICO|ÚNICO)|UNICO|ÚNICO)$", re.I)


def contenedor_producto(soup):
    """Bloque del producto PRINCIPAL (no el de los carruseles de 'relacionados')."""
    return (soup.select_one('[data-store="product-detail"][data-variants]')
            or soup.select_one("#single-product[data-variants]")
            or soup.find(attrs={"data-variants": True}))


def leer_variantes(cont):
    if cont is None:
        return []
    try:
        return json.loads(html.unescape(cont["data-variants"]))
    except (json.JSONDecodeError, KeyError):
        return []


def nombres_opciones(soup):
    """{0: 'talle', 1: 'puntos', ...} leído de las etiquetas de cada grupo de variantes."""
    res = {}
    for g in soup.select(".js-product-variants-group"):
        idx, lab = g.get("data-variation-id"), g.select_one("label")
        if idx and idx.isdigit() and lab:
            res[int(idx)] = lab.get_text(strip=True).lower()
    return res


def meta(soup, prop):
    t = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return (t.get("content") or "").strip() if t else ""


def separar_opciones(v, nombres):
    """Devuelve (talle, color) ignorando la opción 'Puntos' (programa Rewards, no es una variante real)."""
    talle = color = None
    for i in range(3):
        val = v.get(f"option{i}")
        if not val:
            continue
        val, nom = str(val).strip(), nombres.get(i, "")
        if "puntos" in nom or "PUNTOS" in val.upper():
            continue
        if "talle" in nom or "size" in nom or "tamaño" in nom:
            talle = val
        elif "color" in nom:
            color = val
        elif talle is None and SIZE_RE.match(val):
            talle = val
        elif color is None and not nom:
            color = val
    return talle, color


def parsear_producto(url):
    r = get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    cont = contenedor_producto(soup)
    variantes = leer_variantes(cont)
    nombres = nombres_opciones(soup)

    # IMPORTANTE: no usar el JSON-LD ni el primer data-product-id de la página: ahí aparecen
    # los productos del carrusel de "relacionados" y se terminaba guardando OTRO producto.
    h1 = soup.select_one("h1.js-product-name") or soup.find("h1")
    nombre = (h1.get_text(strip=True) if h1 else "") or meta(soup, "og:title")

    pid = next((v.get("product_id") for v in variantes if v.get("product_id")), None)
    if pid is None:
        mid = re.search(r"LS\.product\s*=\s*\{[^}]*?\"id\":\s*(\d+)", r.text)
        pid = int(mid.group(1)) if mid else None

    desc_nodo = soup.select_one("#product-description") or soup.select_one(".product-description")
    descripcion = desc_nodo.get_text(" ", strip=True) if desc_nodo else meta(soup, "og:description")

    imagenes = []
    for a in soup.select("a.js-product-slide-link[href]"):
        u = a["href"]
        if u not in imagenes:
            imagenes.append(u)
    if not imagenes:
        og = meta(soup, "og:image:secure_url") or meta(soup, "og:image")
        imagenes = [og] if og else []

    vars_limpias = []
    for v in variantes:
        if v.get("is_visible") is False:
            continue
        talle, color = separar_opciones(v, nombres)
        vars_limpias.append({
            "variant_id": v.get("id"),
            "sku": v.get("sku"),
            "talle": talle,
            "color": color,
            "opciones": " / ".join(x for x in (talle, color) if x),
            "precio": v.get("price_number"),
            "precio_tachado": v.get("compare_at_price_number"),
            "stock": v.get("stock"),            # None = stock ilimitado
            "disponible": v.get("available"),
            "imagen": v.get("image_url"),
        })

    return {
        "product_id": pid,
        "url": url,
        "nombre": nombre,
        "marca": MARCA,
        "descripcion": descripcion,
        "imagenes": imagenes,
        "variantes": vars_limpias,
    }


# ---------- 3. Feed XML para Google Merchant Center ----------

MARCA = "Shaka Cmmnty"
GENERO = "male"        # male / female / unisex: ajustalo según el público de la marca
EDAD = "adult"
MONEDA = "ARS"
INCLUIR_SIN_STOCK = False   # False: se omiten los productos con TODAS las variantes agotadas
                            # (los talles agotados de un producto con stock sí se publican como out_of_stock)

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
    """Fecha de entrega indicada en el nombre ('a partir del 30/11') como datetime, o None."""
    m = re.search(r"a partir del?\s*(\d{1,2})/(\d{1,2})", nombre, flags=re.I)
    if not m:
        return None
    hoy = datetime.now()
    dia, mes = int(m.group(1)), int(m.group(2))
    anio = hoy.year + (1 if (mes, dia) < (hoy.month, hoy.day) and hoy.month - mes > 6 else 0)
    try:
        return datetime(anio, mes, dia)
    except ValueError:
        return None


def url_absoluta(u):
    if not u:
        return None
    return "https:" + u if u.startswith("//") else u


def precio_fmt(n):
    return f"{float(n):.2f} {MONEDA}"


CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def esc(texto):
    from xml.sax.saxutils import escape
    return escape(CTRL.sub("", str(texto)))


# Mismos 4 parámetros UTM, en el mismo orden, que el feed de referencia (Uniwork)
UTM = "utm_source=Google%20Shopping&utm_campaign=feed%20torem&utm_medium=cpc&utm_term=adtribes"


def linea_xml(k, val):
    if k == "g:description":   # CDATA, como en el feed de referencia
        return f"      <{k}><![CDATA[{CTRL.sub('', str(val)).replace(']]>', ']]&gt;')}]]></{k}>"
    return f"      <{k}>{esc(val)}</{k}>"


def disponibilidad_variante(v, preorder):
    if v.get("disponible") is False or v.get("stock") == 0:
        return "out_of_stock"
    return "preorder" if preorder else "in_stock"


def generar_google_xml(productos, ruta):
    items, items_ref, omitidos = [], [], 0
    ahora = datetime.now()
    for p in productos:
        nombre_orig = p["nombre"] or ""
        if not nombre_orig or "GIFT" in nombre_orig.upper():
            continue  # las gift cards no se pueden publicitar en Shopping
        nombre = limpiar_nombre(nombre_orig)
        singular, tipo, categoria = clasificar(nombre_orig)
        color_nombre = extraer_color(nombre)

        # Preventa: solo es 'preorder' si la fecha de entrega todavía no pasó
        # (Google rechaza preorder con availability_date en el pasado)
        es_preventa = bool(re.search(r"PRE-?VENTA", nombre_orig, flags=re.I))
        f = fecha_preventa(nombre_orig) if es_preventa else None
        preorder = bool(f and f > ahora)
        fecha_disp = f.strftime("%Y-%m-%dT00:00-03:00") if preorder else None

        desc = (p["descripcion"] or "").strip()
        if not desc or desc.startswith("Comprá online"):
            desc = f"{nombre}. {singular} de {MARCA}."
        imagenes = [url_absoluta(i) for i in p["imagenes"] if i]

        variantes = [v for v in p["variantes"] if v.get("precio")]
        if not INCLUIR_SIN_STOCK and all(disponibilidad_variante(v, False) == "out_of_stock"
                                         for v in variantes):
            omitidos += 1
            continue

        for v in variantes:
            precio_actual, tachado = v["precio"], v.get("precio_tachado")
            talle = (v.get("talle") or "").strip()
            if talle.upper() in ("TALLE UNICO", "TALLE ÚNICO", "UNICO", "ÚNICO"):
                talle = "Único"
            color = (v.get("color") or "").strip().capitalize() or color_nombre

            # Título como en el feed de referencia: nombre + talle (el color ya viene en el nombre)
            titulo = f"{nombre} {talle}".strip() if talle else nombre

            # Link a la variante concreta + UTM (igual que el feed de referencia)
            link_variante = f"{p['url']}?variant={v['variant_id']}" if v.get("variant_id") else p["url"]
            link = f"{link_variante}&{UTM}" if v.get("variant_id") else f"{p['url']}?{UTM}"

            imagen = url_absoluta(v.get("imagen")) or (imagenes[0] if imagenes else None)
            if not imagen:
                continue  # Google rechaza productos sin imagen
            extras = [i for i in imagenes if i != imagen][:10]

            disp = disponibilidad_variante(v, preorder)
            campos = [
                ("g:id", v.get("variant_id") or f"{p['product_id']}-{talle}"),
                ("g:title", titulo[:150]),
                ("g:description", desc[:5000]),
                ("g:link", link),
                ("g:image_link", imagen),
                *[("g:additional_image_link", i) for i in extras],
                ("g:availability", disp),
                ("g:availability_date", fecha_disp if disp == "preorder" else None),
                ("g:price", None),   # se completa abajo (price / sale_price)
                ("g:google_product_category", categoria),
                ("g:product_type", f"Home > {tipo}"),
                ("g:condition", "new"),
                ("g:item_group_id", p["product_id"]),
                ("g:brand", MARCA),
                ("g:gender", GENERO),
                ("g:age_group", EDAD),
                ("g:size", talle or None),
                ("g:color", color),
                ("g:identifier_exists", "no"),
            ]
            # El SKU de Tiendanube se repite entre productos: no sirve como MPN.
            i = next(n for n, (k, _) in enumerate(campos) if k == "g:price")
            if tachado and float(tachado) > float(precio_actual):
                campos[i:i + 1] = [("g:price", precio_fmt(tachado)),
                                   ("g:sale_price", precio_fmt(precio_actual))]
            else:
                campos[i] = ("g:price", precio_fmt(precio_actual))

            items.append(item_xml(campos))

            # Feed "referencia": MISMA composición que el feed de Uniwork (13 campos, mismo orden)
            ref = [
                ("g:id", v.get("variant_id") or f"{p['product_id']}-{talle}"),
                ("g:title", titulo[:150]),
                ("g:description", desc[:5000]),
                ("g:link", link),
                ("g:image_link", imagen),
                ("g:checkout_link_template", link_variante),
                ("g:availability", "out_of_stock" if disp == "out_of_stock" else "in_stock"),
                ("g:price", precio_fmt(precio_actual)),   # precio final que paga el cliente
                ("g:google_product_category", categoria),
                ("g:product_type", f"Home > {tipo}"),
                ("g:condition", "new"),
                ("g:item_group_id", p["product_id"]),
                ("g:identifier_exists", "no"),
            ]
            items_ref.append(item_xml(ref))

    escribir_rss(items_ref, ruta)                                  # feed.xml = formato Uniwork
    escribir_rss(items, ruta.replace(".xml", "_google.xml"))      # feed_google.xml = completo para Google
    print(f"{ruta}: {len(items_ref)} items | {ruta.replace('.xml', '_google.xml')}: {len(items)} items "
          f"({omitidos} productos agotados omitidos)")


def item_xml(campos):
    lineas = [linea_xml(k, val) for k, val in campos if val]
    return "    <item>\n" + "\n".join(lineas) + "\n    </item>"


def escribir_rss(items, ruta):
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">\n'
        "  <channel>\n"
        f"    <title>{esc(MARCA)}</title>\n"
        f"    <link>{BASE}</link>\n"
        f"    <description>Feed de productos de {esc(MARCA)}</description>\n"
        + "\n".join(items) +
        "\n  </channel>\n</rss>\n"
    )
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(xml)


# ---------- 4. Escribir los feeds ----------

def main():
    inicio = time.time()
    r = get(BASE)
    print(f"Home: {'OK' if r else 'FALLÓ'} | estados HTTP: {ESTADOS}", flush=True)
    urls = urls_desde_sitemap()
    print(f"Sitemap: {len(urls)} productos", flush=True)
    if not urls:
        urls = urls_desde_listado()
        print(f"Listado (fallback): {len(urls)} productos", flush=True)

    def tarea(url):
        if time.time() - inicio > TIEMPO_MAX:
            return None
        p = parsear_producto(url)
        time.sleep(DELAY)
        return p

    productos = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, p in enumerate(pool.map(tarea, sorted(urls)), 1):
            if p:
                productos.append(p)
            if i % 25 == 0:
                print(f"  {i}/{len(urls)} procesados ({time.time() - inicio:.0f}s)")

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
                    "imagen": url_absoluta(v["imagen"] or (p["imagenes"][0] if p["imagenes"] else "")) or "",
                })

    generar_google_xml(productos, "feed.xml")

    if time.time() - inicio > TIEMPO_MAX:
        print("AVISO: se alcanzó el tiempo máximo, el feed quedó parcial", flush=True)
    print(f"Estados HTTP: {ESTADOS}", flush=True)
    print(f"Listo: {len(productos)} productos en {time.time() - inicio:.0f}s", flush=True)


if __name__ == "__main__":
    main()
