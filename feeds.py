"""
Feeds universales de productos para Google Merchant Center y asistentes de IA.

Lee la lista de tiendas de tiendas.json, detecta la plataforma de cada una y saca los datos nativos
de la mejor fuente disponible:

  Shopify      /products.json (catálogo completo) + /products/<producto>.js (códigos de barras)
  WooCommerce  Store API pública /wp-json/wc/store/v1/products (+ cada variación)
  Tiendanube   atributo data-variants de cada página de producto (SKU, stock, precios, talles)
  Luna         JSON interno de cada página de producto (SKU, stock, pre-orden, fotos)
  VTEX         API pública /api/catalog_system/pub/products/search (EAN, stock, especificaciones)
  Magento 2    API GraphQL /graphql
  Otras        datos estructurados de Google (JSON-LD) de cada página del sitemap

Por cada tienda escribe:
  feeds/<tienda>.xml   feed completo para Google Merchant Center
  feeds/<tienda>.json  versión para asistentes de IA (un objeto por producto, con variantes y ficha técnica)
  datos/<tienda>.json  datos crudos y caché (para no repetir consultas en cada corrida)
y además index.html (resumen de todas las tiendas) y feeds/estado.json.

Uso:  pip install requests beautifulsoup4
      python feeds.py              # todas las tiendas activas
      python feeds.py giani fauna  # solo esas
"""
import html
import json
import os
import re
import sys
import time
import traceback
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

CONFIG = "tiendas.json"
DIR_FEEDS = "feeds"
DIR_DATOS = "datos"
DELAY = 0.4              # pausa de cada worker entre requests (ser amable con las tiendas)
WORKERS = 3
TIMEOUT = 25
TIEMPO_MAX_MIN = 8       # minutos por tienda (se puede cambiar por tienda en tiendas.json)
MIN_RATIO = 0.7          # si hoy hay menos del 70% de los productos de la corrida anterior, no se pisa el feed
CONSERVAR_HORAS = 72     # un producto que no se pudo leer conserva sus datos anteriores hasta 72 h
UA = "Mozilla/5.0 (compatible; ProductFeedBot/1.0)"


def ahora():
    return datetime.now(timezone.utc)


def log(tienda, msg):
    print(f"[{tienda}] {msg}", flush=True)


# =============================================================== HTTP ===

class Cliente:
    """Sesión HTTP por tienda: reintentos, freno ante 429 y presupuesto de tiempo."""

    def __init__(self, base, tiempo_max_s):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "es,en;q=0.8"})
        self.inicio = time.time()
        self.tiempo_max = tiempo_max_s
        self.estados = Counter()
        self.freno = 0.0

    def queda_tiempo(self, margen=0):
        return time.time() - self.inicio < self.tiempo_max - margen

    def get(self, url, json_=False, intentos=4, **kw):
        url = urljoin(self.base + "/", url)
        for i in range(intentos):
            if time.time() - self.inicio > self.tiempo_max + 90:
                return None
            espera = self.freno - time.time()
            if espera > 0:
                time.sleep(espera)
            try:
                r = self.s.get(url, timeout=TIMEOUT, **kw)
            except requests.RequestException:
                self.estados["error"] += 1
                time.sleep(2 * (i + 1))
                continue
            self.estados[r.status_code] += 1
            if r.status_code == 200:
                time.sleep(DELAY)
                if not json_:
                    return r
                try:
                    return r.json()
                except ValueError:
                    return None
            if r.status_code in (429, 503, 502, 500, 403):
                ra = r.headers.get("Retry-After", "")
                pausa = min(int(float(ra)) if re.fullmatch(r"\d+(\.\d+)?", ra) else 8 * (i + 1), 60)
                self.freno = max(self.freno, time.time() + pausa)
                continue
            return None
        return None


# ============================================================= TEXTO ===

def sin_acentos(t):
    return "".join(c for c in unicodedata.normalize("NFKD", t or "") if not unicodedata.combining(c))


def limpiar(t):
    t = html.unescape(html.unescape(str(t or ""))).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()


def html_a_lineas(h):
    """HTML -> líneas de texto. Une los cortes de línea que quedan en medio de una frase."""
    t = re.sub(r"<\s*br\s*/?\s*>", "\n", h or "", flags=re.I)
    t = re.sub(r"<\s*/t[dh]\s*>", ": ", t, flags=re.I)
    t = re.sub(r"<\s*/?\s*(p|li|div|h[1-6]|tr|ul|ol|table|section)\b[^>]*>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(html.unescape(t)).replace("\xa0", " ")
    lineas = []
    for linea in t.split("\n"):
        linea = re.sub(r"[ \t\r]+", " ", linea).strip(" \t:•·*")
        if not linea:
            continue
        if lineas and linea[0].islower() and not re.search(r"[.!?:]$", lineas[-1]):
            lineas[-1] += " " + linea          # "El\ncolibrí" -> "El colibrí"
        else:
            lineas.append(linea)
    return lineas


TITULOS_FICHA = {"caracteristicas del producto", "caracteristicas", "especificaciones", "ficha tecnica",
                 "detalles", "detalles del producto", "especificaciones tecnicas"}
RE_SPEC = re.compile(r"^([^:]{2,40}):\s*(.{1,200})$")


def separar_ficha(lineas, quitar=()):
    """Separa 'Clave: valor' (ficha técnica) del texto descriptivo. Elimina párrafos repetidos o no deseados."""
    texto, specs, vistos = [], {}, set()
    for linea in lineas:
        clave = sin_acentos(linea.lower())
        if clave in vistos or any(q.lower() in linea.lower() for q in quitar):
            continue
        vistos.add(clave)
        if clave.rstrip(": ") in TITULOS_FICHA:
            continue
        m = RE_SPEC.match(linea)
        if m and len(m.group(1).split()) <= 5 and not m.group(1).lower().startswith("http"):
            specs.setdefault(m.group(1).strip(), m.group(2).strip())
        else:
            texto.append(linea)
    return "\n\n".join(texto), specs


MINUSCULAS = {"de", "del", "y", "con", "en", "la", "el", "a", "para", "por", "sin", "e", "o", "x"}
UNIDADES = {"cm", "mm", "m", "mts", "kg", "kgs", "g", "gr", "ml", "l", "cc", "oz"}
FORMA_FIJA = {"G-SHOCK": "G-Shock", "BOSS": "BOSS", "Q&Q": "Q&Q", "USB": "USB", "LED": "LED", "UV": "UV",
              "XXS": "XXS", "XS": "XS", "S": "S", "M": "M", "L": "L", "XL": "XL", "XXL": "XXL", "XXXL": "XXXL"}


def titulo_lindo(t):
    """Solo corrige títulos escritos casi todo en mayúsculas: 'RELOJ CASIO CASIO F-91W' -> 'Reloj Casio F-91W'."""
    t = limpiar(t)
    letras = [c for c in t if c.isalpha()]
    if not letras or sum(c.isupper() for c in letras) / len(letras) < 0.7:
        return t
    salida, anterior = [], None
    for i, p in enumerate(t.split(" ")):
        if anterior and p.upper() == anterior.upper() and p not in ("-", "|", "/"):
            continue
        anterior = p
        base = p.strip("(),.")
        if p.upper() in FORMA_FIJA:
            salida.append(FORMA_FIJA[p.upper()])
        elif re.fullmatch(r"[\d.,]+(" + "|".join(UNIDADES) + r")", base.lower()) or base.lower() in UNIDADES:
            salida.append(p.lower())
        elif any(c.isdigit() for c in p):
            salida.append(p.upper())
        elif len(base) <= 3 and base.isalpha() and not re.search(r"[AEIOUÁÉÍÓÚaeiouáéíóú]", base):
            salida.append(p.upper())
        elif i and p.lower() in MINUSCULAS:
            salida.append(p.lower())
        else:
            salida.append(p[:1].upper() + p[1:].lower())
    return " ".join(salida)


def num(v):
    """'330' -> 330 | '2.500' (punto de miles) -> 2500 | '1.250,50' -> 1250.5 | 129.9 -> 129.9"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"[^\d.,-]", "", str(v))
    if not t:
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".") if re.fullmatch(r"-?\d+,\d{1,2}", t) else t.replace(",", "")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None


def gtin_valido(codigo):
    codigo = re.sub(r"\D", "", str(codigo or ""))
    if len(codigo) not in (8, 12, 13, 14) or set(codigo) == {"0"}:
        return None
    d = [int(x) for x in codigo]
    control = d.pop()
    suma = sum(x * (3 if i % 2 == 0 else 1) for i, x in enumerate(reversed(d)))
    return codigo if (10 - suma % 10) % 10 == control else None


def url_abs(u, base):
    if not u:
        return None
    u = u.strip()
    if u.startswith("//"):
        return "https:" + u
    return urljoin(base + "/", u)


# ======================================================= CLASIFICACIÓN ===

CATEGORIAS_GOOGLE = [
    (r"\b(correa|malla|pulso)s? (de|para) reloj", "Apparel & Accessories > Jewelry > Watch Accessories > Watch Bands"),
    (r"\breloj|\brelojes\b|\bsmartwatch", "Apparel & Accessories > Jewelry > Watches"),
    (r"\b(gafas|lentes|anteojos) de sol\b|\bsunglasses|\bgafas\b", "Apparel & Accessories > Clothing Accessories > Sunglasses"),
    (r"\bzapatill|\bsneaker|\bzapato|\bsandalia|\bbotas?\b|\bbotin|\bmocasin|\bbabucha|\bojota|\bchancla|"
     r"\bbailarina|\balpargata|\bpantufla", "Apparel & Accessories > Shoes"),
    (r"\bbilletera|\bmonedero|\btarjetero", "Apparel & Accessories > Handbags, Wallets & Cases > Wallets & Money Clips"),
    (r"\bmochila", "Luggage & Bags > Backpacks"),
    (r"\bbolso de viaje|\bvalija|\bmaleta", "Luggage & Bags > Duffel Bags"),
    (r"\bcartera|\bbolso|\bbandolera|\bbanano|\brinonera|\btote\b", "Apparel & Accessories > Handbags, Wallets & Cases > Handbags"),
    (r"\bmedias\b|\bcalcetin", "Apparel & Accessories > Clothing > Underwear & Socks > Socks"),
    (r"\bgorra|\bgorro|\bsombrero|\bboina|\bpiluso", "Apparel & Accessories > Clothing Accessories > Hats"),
    (r"\bcinturon", "Apparel & Accessories > Clothing Accessories > Belts"),
    (r"\banillo", "Apparel & Accessories > Jewelry > Rings"),
    (r"\bcollar|\bcadena\b|\bgargantilla", "Apparel & Accessories > Jewelry > Necklaces"),
    (r"\bpulsera|\bbrazalete", "Apparel & Accessories > Jewelry > Bracelets"),
    (r"\baretes?\b|\baros\b|\bpendientes\b|\btopos\b", "Apparel & Accessories > Jewelry > Earrings"),
    (r"\bcampera|\bchaqueta|\bparka|\bchaleco|\btapado|\babrigo|\bblazer|\bimpermeable",
     "Apparel & Accessories > Clothing > Outerwear"),
    (r"\bremera|\bcamiseta|\bcamisa|\bpolo\b|\bmusculosa|\btop\b|\bblusa|\bbuzo|\bhoodie|\bsweater|\bsueter|"
     r"\bcardigan|\btee\b|\bt-shirt|\bcrewneck", "Apparel & Accessories > Clothing > Shirts & Tops"),
    (r"\bpantalon|\bjeans?\b|\bjogger|\bcalza\b|\bleggin|\bcargo\b", "Apparel & Accessories > Clothing > Pants"),
    (r"\bshorts?\b|\bbermuda", "Apparel & Accessories > Clothing > Shorts"),
    (r"\bvestido", "Apparel & Accessories > Clothing > Dresses"),
    (r"\bfalda|\bpollera", "Apparel & Accessories > Clothing > Skirts"),
    (r"\bbikini|\btraje de bano|\bmalla\b", "Apparel & Accessories > Clothing > Swimwear"),
    (r"\bescultura|\bestatua|\bfigura decorativa", "Home & Garden > Decor > Artwork > Sculptures & Statues"),
]


def categoria_google(*textos):
    for texto in textos:
        t = sin_acentos((texto or "").lower())
        for patron, cat in CATEGORIAS_GOOGLE:
            if re.search(patron, t):
                return cat
    return None


def detectar_genero(texto):
    t = sin_acentos((texto or "").lower())
    if re.search(r"\bunisex", t):
        return "unisex"
    h = re.search(r"\b(hombres?|caballeros?|masculino|varon|men|male)\b", t)
    m = re.search(r"\b(mujer(es)?|damas?|femenino|women|female)\b", t)
    if h and m:
        return "unisex"
    return "male" if h else "female" if m else None


def es_infantil(texto):
    return bool(re.search(r"\b(ninos?|ninas?|infantil|bebes?|kids|junior|nene|nena)\b", sin_acentos((texto or "").lower())))


TRIVIALES = {"talle unico", "talla unica", "unico", "unica", "u", "tu", "default", "default title", "one size"}
RE_TALLE = re.compile(r"talle|talla|size|n[uú]mero|calce", re.I)
RE_COLOR = re.compile(r"color|colour", re.I)

COLORES = ("negro|blanco|azul|rojo|verde|amarillo|rosa|rosado|fucsia|violeta|lila|gris|celeste|naranja|marron|"
           "beige|crudo|bordo|coral|aqua|agua|menta|turquesa|dorado|plateado|plata|oro|salmon|natural|camel|nude|"
           "multicolor|khaki|caqui|mostaza|terracota|petroleo|vino|lima|fluo|oliva|chocolate|arena|hueso|jean|"
           "taupe|cafe|marino|indigo|grafito|perla|cobre|bronce|lavanda|uva|cereza|arcilla|tostado|habano|"
           "black|white|blue|green|yellow|pink|purple|grey|gray|orange|brown|navy|silver|gold")
RE_ES_COLOR = re.compile(r"\b(" + COLORES + r")s?\b", re.I)
RE_ES_TALLE = re.compile(r"^(t\.?\s?\d{1,3}|talles?\s.*|tallas?\s.*|\d{1,3}([.,]\d)?|\d{1,3}\s?(-|/)\s?\d{1,3}|"
                         r"x{0,3}s|m|x{0,3}l|\d?x[sl]|jr|sr|junior|senior|u|tu|unico|unica|small|medium|large|"
                         r"chico|mediano|grande|\d{2,3}\s?(cm|mm))$", re.I)


def talle_y_color(opciones):
    """Decide talle y color por el nombre de la opción y lo corrige mirando el valor:
    'Color: T8 / Talle: Azul' -> talle T8, color Azul."""
    talle = next((x for k, x in opciones.items() if RE_TALLE.search(k)), None)
    color = next((x for k, x in opciones.items() if RE_COLOR.search(k)), None)
    parece_talle = lambda x: bool(x) and bool(RE_ES_TALLE.match(sin_acentos(x.strip().lower())))
    parece_color = lambda x: bool(x) and bool(RE_ES_COLOR.search(sin_acentos(x.lower()))) and not parece_talle(x)
    if parece_talle(color) and not parece_color(color) and (talle is None or parece_color(talle)):
        talle, color = color, talle            # vinieron invertidos
    elif parece_color(talle) and not parece_talle(talle) and (color is None or parece_talle(color)):
        talle, color = color, talle
    for x in opciones.values():               # opciones sin nombre reconocible: se clasifican por el valor
        if talle is None and x != color and parece_talle(x):
            talle = x
        elif color is None and x != talle and parece_color(x):
            color = x
    return talle, color


# ===================================================== MODELO COMÚN ===
# producto = {id, url, titulo, descripcion, marca, categorias[], tags[], imagenes[], specs{}, variantes[]}
# variante = {id, sku, gtin, opciones{}, precio, precio_anterior, moneda, disponible, stock, preorden, url, imagen}

def variante(**kw):
    v = {"id": None, "sku": "", "gtin": "", "opciones": {}, "precio": None, "precio_anterior": None,
         "moneda": "", "disponible": True, "stock": None, "preorden": False, "url": None, "imagen": None}
    v.update(kw)
    v["id"] = str(v["id"]) if v["id"] is not None else None
    v["sku"] = (v["sku"] or "").strip()
    if v["precio_anterior"] and v["precio"] and v["precio_anterior"] <= v["precio"]:
        v["precio_anterior"] = None
    return v


def sitemap_urls(c, filtro):
    urls, pendientes, vistos = set(), ["/sitemap.xml", "/sitemap_index.xml"], set()
    while pendientes and len(vistos) < 200:
        sm = pendientes.pop()
        if sm in vistos:
            continue
        vistos.add(sm)
        r = c.get(sm)
        if not r:
            continue
        for loc in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>", r.text):
            loc = html.unescape(loc).strip()
            if re.search(r"\.xml(\.gz)?(\?|$)", loc):
                pendientes.append(loc)
            elif filtro(loc):
                urls.add(loc.split("#")[0])
    return urls


def rotar_paginas(c, tienda, urls, previos, parser):
    """Lee páginas de producto empezando por las nuevas y las menos actualizadas, dentro del tiempo
    disponible. Las que no alcanza a leer conservan sus datos anteriores (hasta CONSERVAR_HORAS)."""
    prev = {p["url"]: p for p in previos}
    orden = [u for u in urls if u not in prev] + sorted((u for u in urls if u in prev),
                                                        key=lambda u: prev[u].get("_leido", ""))
    resultado, vistos, pendientes = {}, set(), orden
    limite = (ahora() - timedelta(hours=CONSERVAR_HORAS)).isoformat()

    def tarea(u):
        if not c.queda_tiempo(margen=45):
            return u, None, False
        try:
            return u, parser(c, u), True
        except Exception as e:  # una página rota no frena al resto
            log(tienda, f"  error leyendo {u}: {type(e).__name__}: {e}")
            return u, None, True

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while pendientes:
            vistos.update(pendientes)
            nuevos = set()
            for u, p, intentado in pool.map(tarea, pendientes):
                if p:
                    p["_leido"] = ahora().isoformat()
                    nuevos |= set(p.pop("_relacionados", []))
                    resultado[p["url"]] = p
                elif u in prev and (not intentado or prev[u].get("_leido", "") > limite):
                    resultado[u] = prev[u]
            pendientes = [u for u in nuevos - vistos if c.queda_tiempo(margen=45)]
    leidos = sum(1 for p in resultado.values() if p.get("_leido", "") > (ahora() - timedelta(minutes=30)).isoformat())
    log(tienda, f"  páginas leídas en esta corrida: {leidos} de {len(resultado)} (el resto conserva datos anteriores)")
    return list(resultado.values())


# ============================================================ SHOPIFY ===

def shopify(c, t, prev):
    crudos = []
    for page in range(1, 41):
        d = c.get(f"/products.json?limit=250&page={page}", json_=True)
        lote = (d or {}).get("products") or []
        if not lote:
            break
        crudos += lote
    moneda = t.get("moneda") or (c.get("/cart.js", json_=True) or {}).get("currency", "")

    # Códigos de barras: /products.json no los trae; /products/<handle>.js sí. Se guardan en caché.
    gtins = dict(prev.get("cache", {}).get("gtin", {}))
    pendientes = [p for p in crudos if any(str(v["id"]) not in gtins for v in p.get("variants", []))]
    if pendientes:
        log(t["slug"], f"  buscando códigos de barras de {len(pendientes)} productos")

        def tarea(p):
            if not c.queda_tiempo(margen=60):
                return p, None
            return p, c.get(f"/products/{p['handle']}.js", json_=True)

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for p, js in pool.map(tarea, pendientes):
                if js:
                    for v in js.get("variants", []):
                        gtins[str(v.get("id"))] = v.get("barcode") or ""

    productos = []
    for p in crudos:
        if "gift" in (p.get("product_type") or "").lower() or "tarjeta de regalo" in (p.get("title") or "").lower():
            continue
        url = f"{c.base}/products/{p['handle']}"
        desc, specs = separar_ficha(html_a_lineas(p.get("body_html")), t.get("quitar_parrafos", []))
        img_var, imagenes = {}, []
        for img in p.get("images") or []:
            src = url_abs(img.get("src"), c.base)
            if src and src not in imagenes:
                imagenes.append(src)
            for vid in img.get("variant_ids") or []:
                img_var[vid] = src
        nombres = [o.get("name", "") for o in p.get("options") or []]
        tags = p.get("tags") or []
        variantes = []
        for v in p.get("variants") or []:
            opciones = {}
            for i, nombre in enumerate(nombres[:3]):
                val = v.get(f"option{i + 1}")
                if val and val != "Default Title":
                    opciones[nombre] = val
            fi = v.get("featured_image")
            variantes.append(variante(
                id=v.get("id"), sku=v.get("sku"), gtin=v.get("barcode") or gtins.get(str(v.get("id")), ""),
                opciones=opciones, precio=num(v.get("price")), precio_anterior=num(v.get("compare_at_price")),
                moneda=moneda, disponible=bool(v.get("available")), url=f"{url}?variant={v.get('id')}",
                imagen=url_abs((fi or {}).get("src"), c.base) if isinstance(fi, dict) else img_var.get(v.get("id"))))
        productos.append({
            "id": str(p.get("id")), "url": url, "titulo": p.get("title", ""), "descripcion": desc,
            "marca": (p.get("vendor") or "").strip(), "categorias": [p["product_type"]] if p.get("product_type") else [],
            "tags": tags if isinstance(tags, list) else [x.strip() for x in tags.split(",")],
            "imagenes": imagenes, "specs": specs, "variantes": variantes})
    return productos, {"gtin": gtins}


# ======================================================== WOOCOMMERCE ===

def woo_base(c):
    for base in ("/wp-json/wc/store/v1/products", "/wp-json/wc/store/products"):
        if isinstance(c.get(base + "?per_page=1", json_=True), list):
            return base
    return None


def woo_precios(pr):
    pr = pr or {}
    menor = int(pr.get("currency_minor_unit", 2) or 0)
    f = lambda x: (int(x) / 10 ** menor) if str(x or "").lstrip("-").isdigit() else None
    return f(pr.get("price")), f(pr.get("regular_price")), pr.get("currency_code", "")


def woocommerce(c, t, prev):
    base = woo_base(c)
    crudos = []
    for page in range(1, 201):
        d = c.get(f"{base}?per_page=100&page={page}", json_=True)
        if not isinstance(d, list) or not d:
            break
        crudos += d
        if len(d) < 100:
            break
    productos = []
    for p in crudos:
        url = p.get("permalink") or ""
        lineas = html_a_lineas(p.get("description") or p.get("short_description"))
        desc, specs = separar_ficha(lineas, t.get("quitar_parrafos", []))
        norm = lambda n: sin_acentos(re.sub(r"^pa_", "", n or "")).lower().replace("-", " ")
        valores, nombre_attr = {}, {}
        for a in p.get("attributes") or []:
            terms = a.get("terms") or []
            valores[norm(a.get("name"))] = {x.get("slug"): x.get("name") for x in terms}
            nombre_attr[norm(a.get("name"))] = a.get("name")
            if not a.get("has_variations") and terms:
                specs.setdefault(a.get("name"), ", ".join(x.get("name") for x in terms))
        marca = next((b.get("name") for b in p.get("brands") or [] if b.get("name")), "") or \
            next((v for k, v in specs.items() if k.lower() in ("marca", "brand")), "")
        imagenes = [url_abs(i.get("src"), c.base) for i in p.get("images") or [] if i.get("src")]
        precio, regular, moneda = woo_precios(p.get("prices"))
        variantes = []
        for var in p.get("variations") or []:
            opciones = {}
            for a in var.get("attributes") or []:
                n, val = norm(a.get("name")), a.get("value") or ""
                opciones[nombre_attr.get(n, a.get("name"))] = valores.get(n, {}).get(val, val)
            datos = c.get(f"{base}/{var.get('id')}", json_=True) if c.queda_tiempo(margen=60) else None
            if isinstance(datos, dict):
                vp, vr, vm = woo_precios(datos.get("prices"))
                img = next((i.get("src") for i in datos.get("images") or [] if i.get("src")), None)
                variantes.append(variante(
                    id=var.get("id"), sku=datos.get("sku"), opciones=opciones, precio=vp, precio_anterior=vr,
                    moneda=vm or moneda, disponible=bool(datos.get("is_in_stock")),
                    preorden=bool(datos.get("is_on_backorder")), stock=datos.get("low_stock_remaining"),
                    url=datos.get("permalink") or url, imagen=url_abs(img, c.base)))
            else:   # sin tiempo para la variación: se usan los datos del producto
                variantes.append(variante(id=var.get("id"), opciones=opciones, precio=precio, precio_anterior=regular,
                                          moneda=moneda, disponible=bool(p.get("is_in_stock")), url=url))
        if not variantes:
            variantes.append(variante(
                id=p.get("id"), sku=p.get("sku"), precio=precio, precio_anterior=regular, moneda=moneda,
                disponible=bool(p.get("is_in_stock")), preorden=bool(p.get("is_on_backorder")),
                stock=p.get("low_stock_remaining"), url=url))
        productos.append({
            "id": str(p.get("id")), "url": url, "titulo": limpiar(p.get("name")), "descripcion": desc,
            "marca": marca, "categorias": [limpiar(x.get("name")) for x in (p.get("categories") or [])[:1]],
            "tags": [x.get("name") for x in p.get("tags") or []], "imagenes": imagenes, "specs": specs,
            "variantes": variantes})
    return productos, {}


# ========================================================= TIENDANUBE ===

def tiendanube(c, t, prev):
    host = urlparse(c.base).netloc.replace("www.", "")

    def es_producto(u):
        pu = urlparse(u)
        partes = [x for x in pu.path.split("/") if x]
        return pu.netloc.replace("www.", "") == host and len(partes) == 2 and partes[0] in ("productos", "produtos", "products")

    urls = sitemap_urls(c, es_producto)
    if not urls:
        for page in range(1, 101):
            r = c.get(f"/productos/?page={page}")
            if not r:
                break
            nuevos = {urljoin(c.base + "/", h).split("?")[0] for h in re.findall(r'href="([^"]+)"', r.text)}
            nuevos = {u for u in nuevos if es_producto(u)} - urls
            if not nuevos:
                break
            urls |= nuevos
    log(t["slug"], f"  {len(urls)} productos en el sitemap")
    ignorar = [x.lower() for x in t.get("ignorar_opciones", ["puntos"])]

    def leer(c, url):
        r = c.get(url)
        if not r:
            return None
        sopa = BeautifulSoup(r.text, "html.parser")
        # Ojo: el JSON-LD y el primer data-variants pueden ser de productos relacionados (carrusel)
        cont = sopa.select_one('[data-store="product-detail"][data-variants]') or \
            sopa.select_one("#single-product[data-variants]") or sopa.find(attrs={"data-variants": True})
        try:
            crudas = json.loads(html.unescape(cont["data-variants"])) if cont else []
        except (ValueError, KeyError):
            crudas = []
        nombres = {}
        for g in sopa.select(".js-product-variants-group"):
            idx, lab = g.get("data-variation-id"), g.select_one("label")
            if idx and idx.isdigit() and lab:
                nombres[int(idx)] = lab.get_text(strip=True).rstrip(":")
        h1 = sopa.select_one("h1.js-product-name") or sopa.find("h1")
        meta = lambda p: (sopa.find("meta", attrs={"property": p}) or sopa.find("meta", attrs={"name": p}) or {}).get("content", "")
        nodo = sopa.select_one("#product-description") or sopa.select_one(".product-description") or \
            sopa.select_one('[data-store="product-description"]')
        desc, specs = separar_ficha(html_a_lineas(nodo.decode_contents() if nodo else meta("og:description")),
                                    t.get("quitar_parrafos", []))
        imagenes = []
        for a in sopa.select("a.js-product-slide-link[href]"):
            u = url_abs(a["href"], c.base)
            if u not in imagenes:
                imagenes.append(u)
        if not imagenes and meta("og:image"):
            imagenes = [url_abs(meta("og:image:secure_url") or meta("og:image"), c.base)]
        migas = [a.get_text(strip=True) for a in sopa.select(".breadcrumbs a, .breadcrumb a, nav[aria-label*=read] a")]
        migas = [m for m in migas if sin_acentos(m.lower()) not in ("inicio", "home", "productos", "tienda")]
        moneda = meta("product:price:currency") or t.get("moneda", "")
        variantes = []
        for v in crudas:
            if v.get("is_visible") is False:
                continue
            opciones = {}
            for i in range(3):
                val, nom = v.get(f"option{i}"), nombres.get(i, f"Opción {i + 1}")
                if val and not any(x in nom.lower() or x in str(val).lower() for x in ignorar):
                    opciones[nom] = str(val).strip()
            variantes.append(variante(
                id=v.get("id"), sku=v.get("sku"), gtin=v.get("barcode") or "", opciones=opciones,
                precio=num(v.get("price_number")), precio_anterior=num(v.get("compare_at_price_number")),
                moneda=moneda, disponible=bool(v.get("available")) and v.get("stock") != 0, stock=v.get("stock"),
                url=f"{url}?variant={v.get('id')}" if len(crudas) > 1 else url, imagen=url_abs(v.get("image_url"), c.base)))
        pid = next((v.get("product_id") for v in crudas if v.get("product_id")), None) or url
        return {"id": str(pid), "url": url, "titulo": (h1.get_text(strip=True) if h1 else meta("og:title")),
                "descripcion": desc, "marca": "", "categorias": migas[-2:], "tags": [], "imagenes": imagenes,
                "specs": specs, "variantes": variantes}

    return rotar_paginas(c, t["slug"], urls, prev.get("productos", []), leer), {}


# =============================================================== LUNA ===

def buscar_json(texto, condicion):
    dec = json.JSONDecoder()
    for fuente in (texto, html.unescape(texto)):
        for m in re.finditer(r'[\[{]\s*\{?\s*"id"\s*:', fuente):
            try:
                obj, _ = dec.raw_decode(fuente, m.start())
            except ValueError:
                continue
            if condicion(obj):
                return obj
    return None


def luna(c, t, prev):
    host = urlparse(c.base).netloc.replace("www.", "")
    re_prod = re.compile(r"^https?://(?:www\.)?" + re.escape(host) + r"/p/([^/?#]+)/(\d+)/(\d+)/?$")
    re_cat = re.compile(r"^https?://(?:www\.)?" + re.escape(host) + r"/([a-z0-9-]+)(?:/([a-z0-9-]+))?/(\d+)/?$")
    excluir = {"p", "c", "cart", "carrito", "about", "contactanos", "checkout", "login"}

    def links(texto):
        return {urljoin(c.base + "/", html.unescape(h)).split("?")[0].split("#")[0].rstrip("/")
                for h in re.findall(r'href=["\']([^"\']+)["\']', texto)} 

    home = c.get("/")
    cats = {(m.group(1), m.group(2)) for u in links(home.text if home else "") for m in [re_cat.match(u)]
            if m and m.group(1) not in excluir}
    nombre_cat = {}
    menu = BeautifulSoup(home.text, "html.parser") if home else None
    if menu:
        for a in menu.find_all("a", href=True):
            m = re_cat.match(urljoin(c.base + "/", a["href"]))
            if m and a.get_text(strip=True):
                nombre_cat[(m.group(1), m.group(2))] = a.get_text(strip=True)
    categoria_de = {}
    for cat, sub in sorted(cats, key=lambda x: (x[1] is not None, x)):
        ruta = f"{cat}/{sub}" if sub else cat
        nombre = [nombre_cat.get((cat, None), cat.replace("-", " ").title())]
        if sub:
            nombre.append(nombre_cat.get((cat, sub), sub.replace("-", " ").title()))
        vistos = set()
        for pag in range(1, 31):
            r = c.get(f"/{ruta}/{pag}")
            nuevos = {u for u in links(r.text if r else "") if re_prod.match(u)} - vistos
            if not nuevos:
                break
            vistos |= nuevos
            for u in nuevos:
                if sub or u not in categoria_de:
                    categoria_de[u] = nombre
    log(t["slug"], f"  {len(categoria_de)} productos en las categorías")

    def leer(c, url):
        r = c.get(url)
        if not r:
            return None
        variantes_raw = buscar_json(r.text, lambda o: isinstance(o, list) and o and isinstance(o[0], dict)
                                    and "sku" in o[0] and "price" in o[0])
        if not variantes_raw:
            return None
        prod = buscar_json(r.text, lambda o: isinstance(o, dict) and set(o) == {"id", "name"}) or {}
        sopa = BeautifulSoup(r.text, "html.parser")
        lineas = [x.strip() for x in sopa.get_text("\n").split("\n") if x.strip()]
        tamanos = []
        for i, x in enumerate(lineas):
            if x.lower() in ("tamaño", "tamaños", "medida", "medidas"):
                for y in lineas[i + 1:i + 12]:
                    if y.lower() in ("guía de talles", "guia de talles", "cantidad", "-", "+", "agregar al carrito"):
                        break
                    tamanos.append(y)
                break
        m = re_prod.match(url)
        nombre = prod.get("name") or variantes_raw[0].get("title") or ""
        primera = variantes_raw[0]
        desc, specs = separar_ficha(html_a_lineas(primera.get("description")), t.get("quitar_parrafos", []))
        imagenes = [f.get("url") for f in primera.get("files") or [] if f.get("url")]
        variantes = []
        for i, v in enumerate(variantes_raw):
            tam = tamanos[i] if len(tamanos) == len(variantes_raw) else (tamanos[0] if len(tamanos) == 1 else "")
            opciones = {"Tamaño": tam} if tam else {}
            titulo_v = (v.get("title") or "").strip()
            if titulo_v and titulo_v.upper() != nombre.upper():
                opciones["Modelo"] = titulo_v
            stock = num(v.get("stock")) or 0
            limitado = bool(v.get("limited_by_stock"))
            simbolo = (v.get("currency_symbol") or "").upper()
            fotos = [f.get("url") for f in v.get("files") or [] if f.get("url")]
            variantes.append(variante(
                id=v.get("id"), sku=v.get("sku"), opciones=opciones, precio=num(v.get("price")),
                precio_anterior=num(v.get("old_price")), moneda="USD" if "U" in simbolo else (t.get("moneda") or "UYU"),
                disponible=stock > 0 or not limitado, preorden=stock <= 0 and limitado and bool(v.get("preorder_stock")),
                stock=int(stock) if limitado else None, url=f"{c.base}/p/{m.group(1)}/{m.group(2)}/{v.get('id')}" if m else url,
                imagen=fotos[0] if fotos else None))
        if nombre.upper().startswith(("PIEZA ÚNICA", "PIEZA UNICA")):
            animal = re.match(r"^([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ ]{2,30}?)\s*[–-]\s*\w", desc)
            if animal:
                nombre = f"{animal.group(1)} {nombre}"
        return {"id": str(prod.get("id") or (m.group(2) if m else url)), "url": url, "titulo": nombre,
                "descripcion": desc, "marca": "", "categorias": categoria_de.get(url, []), "tags": [],
                "imagenes": imagenes, "specs": specs, "variantes": variantes,
                "_relacionados": [u for u in links(r.text) if re_prod.match(u)]}

    return rotar_paginas(c, t["slug"], list(categoria_de), prev.get("productos", []), leer), {}


# =============================================================== VTEX ===

def vtex(c, t, prev):
    crudos = {}

    def recorrer(fq=None):
        for desde in range(0, 2500, 50):
            q = f"/api/catalog_system/pub/products/search?_from={desde}&_to={desde + 49}" + (f"&fq={fq}" if fq else "")
            d = c.get(q, json_=True)
            if not isinstance(d, list) or not d:
                return False
            for p in d:
                crudos[p.get("productId")] = p
            if len(d) < 50:
                return False
        return True   # llegó al tope de 2500 de la API: hay que recorrer por categorías

    if recorrer():
        arbol = c.get("/api/catalog_system/pub/category/tree/3", json_=True) or []

        def explorar(nodo, ruta):
            ruta = ruta + [str(nodo.get("id"))]
            if recorrer("C:/" + "/".join(ruta) + "/"):
                for hijo in nodo.get("children") or []:
                    explorar(hijo, ruta)

        for nodo in arbol:
            explorar(nodo, [])
    productos = []
    for p in crudos.values():
        desc, specs = separar_ficha(html_a_lineas(p.get("description")), t.get("quitar_parrafos", []))
        nombres_var = {n for it in p.get("items") or [] for n in it.get("variations") or []}
        for nombre in p.get("allSpecifications") or []:
            vals = p.get(nombre)
            if vals and nombre not in nombres_var:
                specs.setdefault(nombre, ", ".join(map(str, vals)))
        cats = sorted(p.get("categories") or [], key=len)
        variantes, imagenes = [], []
        for it in p.get("items") or []:
            oferta = next((s.get("commertialOffer") for s in it.get("sellers") or [] if s.get("commertialOffer")), {})
            fotos = [i.get("imageUrl") for i in it.get("images") or [] if i.get("imageUrl")]
            imagenes += [f for f in fotos if f not in imagenes]
            ref = next((r.get("Value") for r in it.get("referenceId") or [] if r.get("Value")), "")
            cantidad = oferta.get("AvailableQuantity")
            variantes.append(variante(
                id=it.get("itemId"), sku=ref or it.get("itemId"), gtin=it.get("ean") or "",
                opciones={n: (it.get(n) or [""])[0] for n in it.get("variations") or []},
                precio=num(oferta.get("Price")), precio_anterior=num(oferta.get("ListPrice")),
                moneda=t.get("moneda", ""), disponible=bool(cantidad) and bool(oferta.get("IsAvailable", True)),
                stock=cantidad, url=f"{p.get('link')}?skuId={it.get('itemId')}", imagen=fotos[0] if fotos else None))
        productos.append({
            "id": str(p.get("productId")), "url": p.get("link"), "titulo": p.get("productName", ""), "descripcion": desc,
            "marca": p.get("brand") or "", "categorias": [x for x in (cats[-1] if cats else "").split("/") if x],
            "tags": [], "imagenes": imagenes, "specs": specs, "variantes": variantes})
    return productos, {}


# ============================================================ MAGENTO ===

CAMPOS_PRECIO = "price_range{minimum_price{regular_price{value currency} final_price{value currency}}}"
CONSULTA_MAGENTO = (
    "{products(%s, pageSize:100, currentPage:%d){page_info{total_pages} items{__typename uid sku name url_key "
    "url_suffix canonical_url description{html} short_description{html} stock_status " + CAMPOS_PRECIO +
    " media_gallery{url disabled} categories{name} ... on ConfigurableProduct{configurable_options{label attribute_code} "
    "variants{attributes{code label} product{uid sku stock_status " + CAMPOS_PRECIO + " media_gallery{url}}}}}}}")


def magento(c, t, prev):
    crudos = []
    for filtro in ('filter:{price:{from:"0"}}', 'search:""', 'search:" "'):
        crudos, total = [], 1
        pagina = 1
        while pagina <= total and pagina <= 100:
            d = c.get("/graphql", json_=True, params={"query": CONSULTA_MAGENTO % (filtro, pagina)})
            prods = ((d or {}).get("data") or {}).get("products") or {}
            if not prods.get("items"):
                break
            crudos += prods["items"]
            total = (prods.get("page_info") or {}).get("total_pages") or 1
            pagina += 1
        if crudos:
            break

    def precio(pr):
        mp = (pr or {}).get("minimum_price") or {}
        final, regular = mp.get("final_price") or {}, mp.get("regular_price") or {}
        return num(final.get("value")), num(regular.get("value")), final.get("currency") or t.get("moneda", "")

    productos = []
    for p in crudos:
        url = p.get("canonical_url") or f"{p.get('url_key')}{p.get('url_suffix') or '.html'}"
        url = url_abs(url, c.base)
        desc, specs = separar_ficha(html_a_lineas((p.get("description") or {}).get("html")
                                                  or (p.get("short_description") or {}).get("html")),
                                    t.get("quitar_parrafos", []))
        imagenes = [m.get("url") for m in p.get("media_gallery") or [] if m.get("url") and not m.get("disabled")]
        variantes = []
        for v in p.get("variants") or []:
            hijo = v.get("product") or {}
            vp, vr, vm = precio(hijo.get("price_range"))
            fotos = [m.get("url") for m in hijo.get("media_gallery") or [] if m.get("url")]
            variantes.append(variante(
                id=hijo.get("uid") or hijo.get("sku"), sku=hijo.get("sku"),
                opciones={a.get("code"): a.get("label") for a in v.get("attributes") or []},
                precio=vp, precio_anterior=vr, moneda=vm, disponible=hijo.get("stock_status") == "IN_STOCK",
                url=url, imagen=fotos[0] if fotos else None))
        if not variantes:
            vp, vr, vm = precio(p.get("price_range"))
            variantes.append(variante(id=p.get("uid") or p.get("sku"), sku=p.get("sku"), precio=vp, precio_anterior=vr,
                                      moneda=vm, disponible=p.get("stock_status") == "IN_STOCK", url=url))
        productos.append({
            "id": str(p.get("uid") or p.get("sku")), "url": url, "titulo": p.get("name", ""), "descripcion": desc,
            "marca": "", "categorias": [x.get("name") for x in (p.get("categories") or [])[-1:]], "tags": [],
            "imagenes": imagenes, "specs": specs, "variantes": variantes})
    return productos, {}


# ================================================ GENÉRICO (JSON-LD) ===

DISPONIBILIDAD_LD = {"instock": "in", "instoreonly": "in", "onlineonly": "in", "limitedavailability": "in",
                     "preorder": "pre", "presale": "pre", "backorder": "pre"}


def nodos_ld(sopa):
    nodos = []
    for s in sopa.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (ValueError, TypeError):
            continue
        pila = data if isinstance(data, list) else [data]
        while pila:
            n = pila.pop()
            if isinstance(n, list):
                pila += n
            elif isinstance(n, dict):
                nodos.append(n)
                pila += n.get("@graph", []) if isinstance(n.get("@graph"), list) else []
    return nodos


def tipo_ld(n):
    tp = n.get("@type")
    return " ".join(tp) if isinstance(tp, list) else str(tp or "")


def generico(c, t, prev):
    patron = re.compile(t.get("patron_productos") or r"/(product|producto|products|productos|p|item|articulo|prod)[/-]|-p-|/p\d", re.I)
    host = urlparse(c.base).netloc.replace("www.", "")
    urls = sitemap_urls(c, lambda u: urlparse(u).netloc.replace("www.", "") == host and bool(patron.search(u)))
    urls = set(list(urls)[: int(t.get("max_productos", 3000))])
    log(t["slug"], f"  {len(urls)} posibles productos en el sitemap")

    def leer(c, url):
        r = c.get(url)
        if not r:
            return None
        sopa = BeautifulSoup(r.text, "html.parser")
        nodos = nodos_ld(sopa)
        prods = [n for n in nodos if re.search(r"\bProduct(Group)?\b", tipo_ld(n))]
        if not prods:
            return None
        h1 = sopa.find("h1")
        nombre_h1 = sin_acentos((h1.get_text(strip=True) if h1 else "").lower())
        prod = next((n for n in prods if (n.get("url") or "").rstrip("/") == url.rstrip("/")), None) or \
            next((n for n in prods if nombre_h1 and sin_acentos(str(n.get("name", "")).lower()) == nombre_h1), None) or prods[0]
        def imgs(x):
            x = x if isinstance(x, list) else [x]
            return [url_abs(i if isinstance(i, str) else (i or {}).get("url") or (i or {}).get("contentUrl"), c.base)
                    for i in x if i]
        hijos = prod.get("hasVariant") if isinstance(prod.get("hasVariant"), list) else [prod]
        variantes = []
        for h in hijos:
            ofertas = h.get("offers") or {}
            if isinstance(ofertas, dict) and isinstance(ofertas.get("offers"), list):
                ofertas = ofertas["offers"]
            for o in ofertas if isinstance(ofertas, list) else [ofertas]:
                disp = sin_acentos(str(o.get("availability", "instock")).lower()).split("/")[-1]
                estado = DISPONIBILIDAD_LD.get(disp, "out")
                opciones = {k: h.get(k) for k in ("size", "color", "material") if isinstance(h.get(k), str)}
                variantes.append(variante(
                    id=o.get("sku") or h.get("sku") or h.get("productID") or url, sku=o.get("sku") or h.get("sku"),
                    gtin=next((str(h.get(k) or o.get(k)) for k in ("gtin13", "gtin12", "gtin14", "gtin8", "gtin")
                               if h.get(k) or o.get(k)), ""),
                    opciones=opciones, precio=num(o.get("price") or o.get("lowPrice")), moneda=o.get("priceCurrency") or t.get("moneda", ""),
                    disponible=estado == "in", preorden=estado == "pre", url=o.get("url") or url,
                    imagen=(imgs(h.get("image")) or [None])[0] if h is not prod else None))
        marca = prod.get("brand")
        marca = marca.get("name") if isinstance(marca, dict) else (marca or "")
        migas = []
        for n in nodos:
            if "BreadcrumbList" in tipo_ld(n):
                migas = [(i.get("name") or (i.get("item") or {}).get("name", "")) if isinstance(i, dict) else ""
                         for i in n.get("itemListElement") or []][1:-1]
        desc, specs = separar_ficha(html_a_lineas(prod.get("description")), t.get("quitar_parrafos", []))
        for pv in prod.get("additionalProperty") or []:
            if isinstance(pv, dict) and pv.get("name") and pv.get("value"):
                specs.setdefault(str(pv["name"]), str(pv["value"]))
        return {"id": str(prod.get("productGroupID") or prod.get("sku") or url), "url": url, "titulo": prod.get("name", ""),
                "descripcion": desc, "marca": str(marca), "categorias": [m for m in migas if m], "tags": [],
                "imagenes": imgs(prod.get("image")), "specs": specs, "variantes": variantes}

    return rotar_paginas(c, t["slug"], urls, prev.get("productos", []), leer), {}


# ========================================================= DETECCIÓN ===

ADAPTADORES = {"shopify": shopify, "woocommerce": woocommerce, "tiendanube": tiendanube, "luna": luna,
               "vtex": vtex, "magento": magento, "generico": generico}


def detectar(c):
    r = c.get("/")
    h = r.text if r else ""
    hl = h.lower()
    if "cfluna.com" in hl:
        return "luna"
    if any(x in hl for x in ("mitiendanube", "nuvemshop", "ls.store", "tiendanube")):
        return "tiendanube"
    if "cdn.shopify.com" in hl or "shopify.shop" in hl:
        return "shopify"
    if "vtex" in hl and isinstance(c.get("/api/catalog_system/pub/products/search?_from=0&_to=0", json_=True), list):
        return "vtex"
    if ("woocommerce" in hl or "/wp-content/" in hl) and woo_base(c):
        return "woocommerce"
    if "magento" in hl or "mage/" in hl or "mage-init" in hl:
        return "magento"
    if isinstance(c.get("/products.json?limit=1", json_=True), dict):
        return "shopify"
    if isinstance(c.get("/api/catalog_system/pub/products/search?_from=0&_to=0", json_=True), list):
        return "vtex"
    if woo_base(c):
        return "woocommerce"
    return "generico"


# ============================================================ SALIDAS ===

SIN_DECIMALES = {"CLP", "PYG", "JPY", "KRW", "VND", "ISK", "UGX", "XAF", "XOF"}


def precio_fmt(n, moneda):
    return f"{n:.0f} {moneda}" if moneda in SIN_DECIMALES else f"{n:.2f} {moneda}"


CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def e(v):
    return escape(CTRL.sub("", str(v)))


def armar_items(t, productos):
    """Convierte los productos al formato de Google: un ítem por variante, con todos los campos posibles."""
    marca_tienda = t.get("marca") or t.get("nombre") or ""
    conteo_sku = Counter(v["sku"] for p in productos for v in p["variantes"] if v.get("sku"))
    items, ids = [], set()
    for p in productos:
        variantes = [v for v in p["variantes"] if v.get("precio") and v["precio"] > 0]
        if not variantes or not p.get("titulo"):
            continue
        titulo_base = titulo_lindo(p["titulo"])
        if titulo_base.lower().startswith(("tarjeta de regalo", "gift card", "giftcard")):
            continue
        cats = [titulo_lindo(c) for c in p.get("categorias") or [] if c]
        contexto = " ".join([p["titulo"]] + cats + list(p.get("tags") or []))
        gcat = t.get("categoria_google") or categoria_google(p["titulo"], " ".join(cats)) or t.get("categoria_google_defecto")
        ropa = bool(gcat) and (gcat.startswith("Apparel & Accessories > Clothing") or gcat.endswith("Shoes"))
        genero = detectar_genero(contexto) or t.get("genero")
        edad = "kids" if es_infantil(contexto) else ("adult" if genero or ropa else None)
        specs = {k: re.sub(r"[.;,]+$", "", str(v).strip()) for k, v in (p.get("specs") or {}).items()
                 if v and not re.match(r"^https?://\S+$", str(v).strip())}
        material = next((v for k, v in specs.items() if re.search(r"material|capellada", k, re.I)), None)
        color_spec = next((v for k, v in specs.items() if RE_COLOR.search(k)), None)
        marca = titulo_lindo(p.get("marca") or marca_tienda)
        desc = p.get("descripcion") or ""
        if specs and not all(str(v) in desc for v in list(specs.values())[:3]):
            desc = (desc + "\n\n" if desc else "") + "Características:\n" + "\n".join(f"- {k}: {v}" for k, v in specs.items())
        grupo = p["id"] if len(variantes) > 1 else None
        for v in variantes:
            imagen = v.get("imagen") or (p.get("imagenes") or [None])[0]
            if not imagen:
                continue
            sku = v.get("sku")
            ident = sku if sku and conteo_sku[sku] == 1 else (v.get("id") or sku)
            if not ident or ident in ids:
                ident = f"{ident}-{v.get('id')}"
            ids.add(ident)
            opciones = {k: titulo_lindo(str(x)) for k, x in (v.get("opciones") or {}).items() if x}
            talle, color = talle_y_color(opciones)
            color = color or color_spec
            extra = [x for x in opciones.values() if sin_acentos(x.lower()) not in TRIVIALES
                     and not re.search(r"(?<!\w)" + re.escape(x.lower()) + r"(?!\w)", titulo_base.lower())]
            titulo = titulo_base + (" - " + " / ".join(extra) if extra else "")
            moneda = v.get("moneda") or t.get("moneda")
            if not moneda:
                continue
            if v.get("disponible"):
                disp = "in_stock"
            elif v.get("preorden"):
                disp = t.get("preorden_como", "preorder")
            else:
                disp = "out_of_stock"
            precios = [("g:price", precio_fmt(v["precio_anterior"], moneda)), ("g:sale_price", precio_fmt(v["precio"], moneda))] \
                if v.get("precio_anterior") else [("g:price", precio_fmt(v["precio"], moneda))]
            gtin = gtin_valido(v.get("gtin"))
            mpn = sku if sku and conteo_sku[sku] == 1 else None
            if gtin:
                identificadores = [("g:gtin", gtin), ("g:mpn", mpn)]
            elif mpn and marca:
                identificadores = [("g:mpn", mpn)]
            else:
                identificadores = [("g:identifier_exists", "no")]
            campos = [
                ("g:id", ident),
                ("g:item_group_id", grupo),
                ("g:title", titulo[:150]),
                ("g:description", (desc or titulo)[:5000]),
                ("g:link", v.get("url") or p["url"]),
                ("g:image_link", imagen),
                *[("g:additional_image_link", i) for i in (p.get("imagenes") or []) if i != imagen][:10],
                ("g:availability", disp),
                *precios,
                ("g:brand", marca),
                *identificadores,
                ("g:condition", "new"),
                ("g:google_product_category", gcat),
                ("g:product_type", " > ".join(cats)),
                ("g:gender", genero),
                ("g:age_group", edad),
                ("g:color", (color or "")[:100] or None),
                ("g:size", (talle or "")[:100] or None),
                ("g:material", (material or "")[:200] or None),
                *[("g:product_detail", ("Características", k[:140], str(val)[:1000])) for k, val in list(specs.items())[:30]],
            ]
            items.append(campos)
    return items


def escribir_xml(t, items, ruta):
    partes = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<rss xmlns:g="http://base.google.com/ns/1.0" version="2.0">', "  <channel>",
              f"    <title>{e(t.get('nombre') or t['slug'])}</title>", f"    <link>{e(t['url'])}</link>",
              "    <description>Catalogo completo de productos</description>"]
    for campos in items:
        partes.append("    <item>")
        for k, v in campos:
            if v in (None, "", []):
                continue
            if k == "g:product_detail":
                sec, nom, val = v
                partes.append(f"      <g:product_detail><g:section_name>{e(sec)}</g:section_name>"
                              f"<g:attribute_name>{e(nom)}</g:attribute_name>"
                              f"<g:attribute_value>{e(val)}</g:attribute_value></g:product_detail>")
            else:
                partes.append(f"      <{k}>{e(v)}</{k}>")
        partes.append("    </item>")
    partes += ["  </channel>", "</rss>", ""]
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("\n".join(partes))


def escribir_json_ia(t, productos, ruta):
    salida = []
    for p in productos:
        vs = [v for v in p["variantes"] if v.get("precio")]
        if not vs:
            continue
        salida.append({
            "titulo": titulo_lindo(p["titulo"]), "url": p["url"], "marca": p.get("marca") or t.get("marca") or t.get("nombre"),
            "categorias": p.get("categorias"), "descripcion": p.get("descripcion"), "caracteristicas": p.get("specs"),
            "precio_desde": min(v["precio"] for v in vs), "moneda": vs[0].get("moneda") or t.get("moneda"),
            "disponible": any(v.get("disponible") for v in vs), "imagenes": (p.get("imagenes") or [])[:3],
            "variantes": [{"sku": v.get("sku"), "opciones": v.get("opciones"), "precio": v.get("precio"),
                           "precio_anterior": v.get("precio_anterior"), "disponible": v.get("disponible"),
                           "preorden": v.get("preorden"), "stock": v.get("stock"), "url": v.get("url")} for v in vs]})
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump({"tienda": t.get("nombre") or t["slug"], "url": t["url"], "generado": ahora().isoformat(timespec="seconds"),
                   "total": len(salida), "productos": salida}, f, ensure_ascii=False, indent=1)


def escribir_index(estado):
    filas = "".join(
        f"<tr><td><a href='{e(s['url'])}'>{e(s['nombre'])}</a></td><td>{e(s.get('plataforma', ''))}</td>"
        f"<td>{s.get('productos', 0)}</td><td>{s.get('items', 0)}</td><td>{e(s.get('actualizado', ''))}</td>"
        f"<td>{'⚠️ ' + e(s['error']) if s.get('error') else '✅'}</td>"
        f"<td>{('❌ ' + str(s['graves']) + ' graves' if s.get('graves') else '✅') + (' · ' + str(s['avisos']) + ' avisos' if s.get('avisos') else '')}</td>"
        f"<td><a href='feeds/{e(slug)}.xml'>XML</a> · <a href='feeds/{e(slug)}.json'>JSON</a></td></tr>"
        for slug, s in sorted(estado.items()))
    pagina = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Feeds de productos</title>
<style>:root{{--bg:#fff;--fg:#1a1a1a;--line:#ddd;--link:#0b57d0}}
@media (prefers-color-scheme: dark){{:root{{--bg:#151515;--fg:#eee;--line:#333;--link:#8ab4f8}}}}
body{{font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:1000px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid var(--line);padding:.5rem;text-align:left}}
a{{color:var(--link)}}.w{{overflow-x:auto}}</style></head><body><h1>Feeds de productos</h1><div class="w"><table>
<tr><th>Tienda</th><th>Plataforma</th><th>Productos</th><th>Ítems</th><th>Actualizado (UTC)</th><th>Estado</th><th>Revisión</th><th>Feeds</th></tr>
{filas}</table></div><p>Detalle de la revisión: <a href="feeds/revision.json">revision.json</a></p></body></html>"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(pagina)


# ======================================================= VALIDACIÓN ===

def item_a_dict(campos):
    d = {}
    for k, v in campos:
        if v not in (None, "", []):
            d.setdefault(k, v)
    return d


def valor_precio(texto):
    try:
        return float(str(texto).split()[0])
    except (ValueError, IndexError):
        return None


def ruta_link(u):
    return urlparse(u or "").path.rstrip("/").lower()


def comparar_con_referencia(t, nuestros):
    """Compara contra el feed nativo de la tienda (campo feed_referencia en tiendas.json), producto por producto."""
    try:
        r = requests.get(t["feed_referencia"], timeout=90, headers={"User-Agent": UA})
        import xml.etree.ElementTree as ET
        raiz = ET.fromstring(r.content)
    except Exception as ex:
        return {"error": f"no se pudo leer el feed de referencia: {type(ex).__name__}"}
    g = "{http://base.google.com/ns/1.0}"
    ref = {}
    for it in raiz.iter("item"):
        val = lambda tag: (it.findtext(g + tag) or it.findtext(tag) or "").strip()
        clave = ruta_link(val("link"))
        p = ref.setdefault(clave, {"titulo": val("title"), "precios": [], "stock": False})
        precio = valor_precio(val("sale_price") or val("price"))
        if precio:
            p["precios"].append(precio)
        p["stock"] |= val("availability").replace(" ", "_") == "in_stock"
    mios = {}
    for d in nuestros:
        clave = ruta_link(d.get("g:link"))
        p = mios.setdefault(clave, {"titulo": d.get("g:title"), "precios": [], "stock": False})
        precio = valor_precio(d.get("g:sale_price") or d.get("g:price"))
        if precio:
            p["precios"].append(precio)
        p["stock"] |= d.get("g:availability") == "in_stock"
    faltan = [ref[k]["titulo"] for k in ref if k not in mios]
    sobran = [mios[k]["titulo"] for k in mios if k not in ref]
    precio_distinto, stock_distinto = [], []
    for k in set(ref) & set(mios):
        a, b = ref[k], mios[k]
        if a["precios"] and b["precios"] and abs(min(a["precios"]) - min(b["precios"])) > 0.01 * min(a["precios"]):
            precio_distinto.append(f"{b['titulo']}: nativo {min(a['precios']):g} / nuestro {min(b['precios']):g}")
        if a["stock"] != b["stock"]:
            stock_distinto.append(f"{b['titulo']}: nativo {'con' if a['stock'] else 'sin'} stock / nuestro {'con' if b['stock'] else 'sin'} stock")
    return {"productos_nativo": len(ref), "productos_nuestro": len(mios), "faltan": faltan[:30], "sobran": sobran[:30],
            "precio_distinto": precio_distinto[:30], "stock_distinto": stock_distinto[:30],
            "totales": {"faltan": len(faltan), "sobran": len(sobran), "precio_distinto": len(precio_distinto),
                        "stock_distinto": len(stock_distinto)}}


def validar(t, items, precios_previos):
    graves, avisos, sospechosos = {}, {}, set()
    agregar = lambda dic, regla, ejemplo: dic.setdefault(regla, []).append(ejemplo)
    datos = [item_a_dict(c) for c in items]
    for iid, n in Counter(d.get("g:id") for d in datos).items():
        if n > 1:
            agregar(graves, "ID repetido", f"{iid} ({n} veces)")
    for d in datos:
        nombre = f"{d.get('g:id')} · {d.get('g:title', '')[:60]}"
        precio = valor_precio(d.get("g:price"))
        oferta = valor_precio(d.get("g:sale_price"))
        if not precio or precio <= 0 or len(str(d.get("g:price", "")).split()) < 2:
            agregar(graves, "Precio en cero o sin moneda", nombre)
        if oferta and precio and oferta >= precio:
            agregar(graves, "Precio de oferta mayor o igual al precio normal", nombre)
        if not d.get("g:image_link"):
            agregar(graves, "Producto sin foto", nombre)
        anterior = precios_previos.get(d.get("g:id"))
        actual = oferta or precio
        if anterior and actual and not (anterior / 3 <= actual <= anterior * 3):
            agregar(graves, "Cambio brusco de precio respecto de la corrida anterior", f"{nombre}: {anterior:g} -> {actual:g}")
            sospechosos.add(d.get("g:id"))
        color, talle = d.get("g:color"), d.get("g:size")
        if color and RE_ES_TALLE.match(sin_acentos(color.lower())):
            agregar(avisos, "El color parece un talle", f"{nombre}: color={color}")
        if talle and RE_ES_COLOR.search(sin_acentos(talle.lower())) and not RE_ES_TALLE.match(sin_acentos(talle.lower())):
            agregar(avisos, "El talle parece un color", f"{nombre}: talle={talle}")
        titulo = d.get("g:title", "")
        letras = [c for c in titulo if c.isalpha()]
        if letras and sum(c.isupper() for c in letras) / len(letras) > 0.8 and len(letras) > 8:
            agregar(avisos, "Título en mayúsculas", nombre)
        if not d.get("g:description") or d.get("g:description") == titulo:
            agregar(avisos, "Sin descripción", nombre)
    resumen = {"graves": {k: {"cantidad": len(v), "ejemplos": v[:10]} for k, v in graves.items()},
               "avisos": {k: {"cantidad": len(v), "ejemplos": v[:10]} for k, v in avisos.items()}}
    if t.get("feed_referencia"):
        resumen["comparacion"] = comparar_con_referencia(t, datos)
    # Un precio sospechoso no reemplaza al último precio confiable: así el error se sigue marcando
    # en cada corrida hasta que se corrija (o hasta que alguien lo confirme borrando datos/<tienda>.json).
    precios = {d.get("g:id"): (precios_previos.get(d.get("g:id")) if d.get("g:id") in sospechosos
                               else valor_precio(d.get("g:sale_price")) or valor_precio(d.get("g:price"))) for d in datos}
    return resumen, precios


# =============================================================== MAIN ===

def cargar_json(ruta, defecto):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defecto


def procesar(t):
    slug = t["slug"]
    t["url"] = t["url"].split("?")[0].rstrip("/")
    c = Cliente(t["url"], float(t.get("tiempo_max_min", TIEMPO_MAX_MIN)) * 60)
    prev = cargar_json(f"{DIR_DATOS}/{slug}.json", {})
    plataforma = t.get("plataforma") or prev.get("plataforma") or detectar(c)
    log(slug, f"plataforma: {plataforma}")
    productos, cache = ADAPTADORES[plataforma](c, t, prev)
    if plataforma != "generico" and not productos and not t.get("plataforma"):
        log(slug, "sin productos con el método nativo: pruebo con datos estructurados (JSON-LD)")
        plataforma, (productos, cache) = "generico", generico(c, t, prev)
    antes = len(prev.get("productos", []))
    if not productos or (antes and len(productos) < MIN_RATIO * antes):
        raise RuntimeError(f"solo {len(productos)} productos (antes {antes}); se mantiene el feed anterior")
    items = armar_items(t, productos)
    revision, precios = validar(t, items, prev.get("precios", {}))
    escribir_xml(t, items, f"{DIR_FEEDS}/{slug}.xml")
    escribir_json_ia(t, productos, f"{DIR_FEEDS}/{slug}.json")
    with open(f"{DIR_DATOS}/{slug}.json", "w", encoding="utf-8") as f:
        json.dump({"plataforma": plataforma, "generado": ahora().isoformat(timespec="seconds"),
                   "productos": productos, "cache": cache, "precios": precios}, f, ensure_ascii=False)
    log(slug, f"{len(productos)} productos, {len(items)} ítems | HTTP {dict(c.estados)} | {time.time() - c.inicio:.0f}s")
    for regla, info in revision["graves"].items():
        log(slug, f"  GRAVE: {regla} ({info['cantidad']}), ej.: {info['ejemplos'][0]}")
    for regla, info in revision["avisos"].items():
        log(slug, f"  aviso: {regla} ({info['cantidad']})")
    return {"plataforma": plataforma, "productos": len(productos), "items": len(items), "revision": revision}


def main():
    os.makedirs(DIR_FEEDS, exist_ok=True)
    os.makedirs(DIR_DATOS, exist_ok=True)
    tiendas = [t for t in cargar_json(CONFIG, []) if t.get("activa", True)]
    if len(sys.argv) > 1:
        tiendas = [t for t in tiendas if t["slug"] in sys.argv[1:]]
    estado = cargar_json(f"{DIR_FEEDS}/estado.json", {})
    revision = cargar_json(f"{DIR_FEEDS}/revision.json", {})
    errores, nuevos_graves = 0, []
    for t in tiendas:
        info = {"nombre": t.get("nombre") or t["slug"], "url": t["url"]}
        try:
            r = procesar(t)
            antes = set(revision.get(t["slug"], {}).get("graves", {}))
            rev = r.pop("revision")
            nuevos_graves += [f"{info['nombre']}: {regla}" for regla in rev["graves"] if regla not in antes]
            revision[t["slug"]] = rev
            info.update(r, actualizado=ahora().strftime("%Y-%m-%d %H:%M"), error=None,
                        graves=sum(v["cantidad"] for v in rev["graves"].values()),
                        avisos=sum(v["cantidad"] for v in rev["avisos"].values()))
        except Exception as ex:
            errores += 1
            log(t["slug"], f"ERROR: {ex}")
            traceback.print_exc()
            info.update({k: v for k, v in estado.get(t["slug"], {}).items()
                         if k in ("plataforma", "productos", "items", "actualizado", "graves", "avisos")})
            info["error"] = str(ex)[:200]
        estado[t["slug"]] = info
    with open(f"{DIR_FEEDS}/estado.json", "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)
    with open(f"{DIR_FEEDS}/revision.json", "w", encoding="utf-8") as f:
        json.dump(revision, f, ensure_ascii=False, indent=2)
    with open(f"{DIR_FEEDS}/alertas.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(nuevos_graves))
    escribir_index(estado)
    escribir_resumen_actions(estado, revision, [t["slug"] for t in tiendas], nuevos_graves)
    print(f"Listo: {len(tiendas) - errores} tiendas OK, {errores} con error, {len(nuevos_graves)} errores graves nuevos", flush=True)


def escribir_resumen_actions(estado, revision, slugs, nuevos_graves):
    """Tabla con el resultado de la revisión en la página de la corrida de GitHub Actions."""
    lineas = ["## Revisión de los feeds", "", "| Tienda | Productos | Errores graves | Avisos |", "|---|---|---|---|"]
    for slug in slugs:
        s = estado.get(slug, {})
        lineas.append(f"| {s.get('nombre', slug)} | {s.get('productos', 0)} | "
                      f"{'❌ ' + str(s['graves']) if s.get('graves') else '✅ 0'} | {s.get('avisos', 0)} |")
    for slug in slugs:
        rev = revision.get(slug, {})
        if rev.get("graves") or rev.get("avisos") or rev.get("comparacion"):
            lineas += ["", f"### {estado.get(slug, {}).get('nombre', slug)}"]
            for tipo, icono in (("graves", "❌"), ("avisos", "⚠️")):
                for regla, info in rev.get(tipo, {}).items():
                    lineas.append(f"- {icono} **{regla}** ({info['cantidad']}): " + "; ".join(info["ejemplos"][:3]))
            comp = rev.get("comparacion")
            if comp and comp.get("totales"):
                tot = comp["totales"]
                lineas.append(f"- 🔍 **Contra el feed nativo**: {comp['productos_nuestro']} productos nuestros vs "
                              f"{comp['productos_nativo']} nativos · faltan {tot['faltan']} · sobran {tot['sobran']} · "
                              f"precio distinto {tot['precio_distinto']} · stock distinto {tot['stock_distinto']}")
            elif comp and comp.get("error"):
                lineas.append(f"- 🔍 {comp['error']}")
    if nuevos_graves:
        lineas += ["", "**Errores graves nuevos en esta corrida:** " + "; ".join(nuevos_graves)]
    texto = "\n".join(lineas) + "\n"
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a", encoding="utf-8") as f:
            f.write(texto)


if __name__ == "__main__":
    main()
