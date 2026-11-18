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


# ---------- 3. Escribir el feed ----------

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

    print(f"Listo: {len(productos)} productos en {time.time() - inicio:.0f}s")


if __name__ == "__main__":
    main()
