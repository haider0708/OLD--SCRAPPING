"""Shared product extraction and data-quality helpers."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser


GTIN_LENGTHS = {8, 12, 13, 14}
_GTIN_RE = re.compile(r"(?<!\d)(\d{8}|\d{12,14})(?!\d)")
_SPACE_RE = re.compile(r"\s+")


def clean_text(value: Any) -> Optional[str]:
    """Normalize text-ish values; return None for empty results."""
    if value is None:
        return None
    text = html_lib.unescape(str(value))
    text = text.replace("\xa0", " ")
    text = _SPACE_RE.sub(" ", text).strip()
    return text or None


def parse_price(value: Any) -> Optional[float]:
    """Parse Tunisian/French price strings without treating decimals as thousands."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = clean_text(value)
    if not text:
        return None

    raw = re.sub(r"[^\d,.\-]", "", text)
    if not raw or raw in {"-", ".", ","}:
        return None

    if "," in raw and "." in raw:
        # The rightmost separator is usually the decimal separator.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    elif raw.count(".") > 1:
        head, tail = raw.rsplit(".", 1)
        raw = head.replace(".", "") + "." + tail

    try:
        return float(raw)
    except ValueError:
        return None


def absolute_url(value: Any, base_url: str) -> Optional[str]:
    url = clean_text(value)
    if not url:
        return None
    if url.startswith("data:") or url.startswith("javascript:"):
        return None
    if url.startswith("//"):
        return "https:" + url
    return url if url.startswith(("http://", "https://")) else urljoin(base_url, url)


def normalize_url(value: Any) -> Optional[str]:
    url = clean_text(value)
    if not url:
        return None
    parts = urlsplit(url)
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def normalize_gtin(value: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) not in GTIN_LENGTHS:
        return None
    if len(set(digits)) == 1:
        return None

    check_digit = int(digits[-1])
    body = digits[:-1]
    total = 0
    for index, char in enumerate(reversed(body)):
        total += int(char) * (3 if index % 2 == 0 else 1)
    expected = (10 - (total % 10)) % 10
    return digits if expected == check_digit else None


def extract_gtins_from_text(text: Any) -> List[str]:
    seen = set()
    out = []
    for match in _GTIN_RE.finditer(str(text or "")):
        gtin = normalize_gtin(match.group(1))
        if gtin and gtin not in seen:
            seen.add(gtin)
            out.append(gtin)
    return out


def availability_from_text(value: Any) -> Tuple[Optional[str], Optional[bool]]:
    text = clean_text(value)
    if not text:
        return None, None

    lower = text.lower()
    if "outofstock" in lower or "out_of_stock" in lower:
        return "Rupture de stock", False
    if "instock" in lower or "in_stock" in lower:
        return "En stock", True
    if "rupture" in lower or "indisponible" in lower or "epuise" in lower or "épuis" in lower:
        return text, False
    if "en stock" in lower or "disponible" in lower or "available" in lower:
        return text, True
    return text, None


def first_text(root: Any, selectors: Iterable[str]) -> Optional[str]:
    for selector in selectors:
        node = root.css_first(selector)
        if node:
            value = clean_text(node.text(strip=True))
            if value:
                return value
    return None


def first_attr(root: Any, selectors: Iterable[str], attrs: Iterable[str]) -> Optional[str]:
    for selector in selectors:
        node = root.css_first(selector)
        if not node:
            continue
        for attr in attrs:
            value = clean_text(node.attributes.get(attr))
            if value:
                return value
    return None


def _walk_json(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _type_names(value: Any) -> List[str]:
    raw = value.get("@type") if isinstance(value, dict) else None
    if isinstance(raw, list):
        return [str(v).lower() for v in raw]
    if raw is None:
        return []
    return [str(raw).lower()]


def _is_product_json(value: Any) -> bool:
    return isinstance(value, dict) and "product" in _type_names(value)


def _jsonld_candidates(html: str) -> List[dict]:
    tree = HTMLParser(html)
    products: List[dict] = []
    for script in tree.css("script[type='application/ld+json']"):
        raw = script.text()
        if not raw:
            continue
        try:
            parsed = json.loads(html_lib.unescape(raw.strip()))
        except (TypeError, json.JSONDecodeError):
            continue
        for obj in _walk_json(parsed):
            if _is_product_json(obj):
                products.append(obj)
    return products


def _candidate_urls(product: dict) -> List[str]:
    urls = []
    for key in ("url", "@id"):
        value = product.get(key)
        if isinstance(value, str):
            urls.append(value)
    main_page = product.get("mainEntityOfPage")
    if isinstance(main_page, dict):
        for key in ("@id", "url"):
            if isinstance(main_page.get(key), str):
                urls.append(main_page[key])
    offers = product.get("offers")
    offer_items = offers if isinstance(offers, list) else [offers]
    for offer in offer_items:
        if isinstance(offer, dict) and isinstance(offer.get("url"), str):
            urls.append(offer["url"])
    return urls


def _select_jsonld_product(products: List[dict], product_url: Optional[str]) -> Optional[dict]:
    if not products:
        return None
    target = normalize_url(product_url)
    if target:
        for product in products:
            for candidate in _candidate_urls(product):
                if normalize_url(candidate) == target:
                    return product
    return products[0]


def _brand_from_json(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return clean_text(value.get("name") or value.get("@id"))
    return clean_text(value)


def _images_from_json(value: Any) -> List[str]:
    images: List[str] = []

    def add(url: Any):
        text = clean_text(url)
        if text and text not in images:
            images.append(text)

    if isinstance(value, str):
        add(value)
    elif isinstance(value, dict):
        add(value.get("url") or value.get("contentUrl") or value.get("image"))
    elif isinstance(value, list):
        for item in value:
            images.extend([u for u in _images_from_json(item) if u not in images])
    return images


def _offer_metadata(offers: Any) -> Dict[str, Any]:
    offer = offers[0] if isinstance(offers, list) and offers else offers
    if not isinstance(offer, dict):
        return {}

    data: Dict[str, Any] = {}
    price = parse_price(offer.get("price"))
    if price is None and isinstance(offer.get("priceSpecification"), dict):
        price = parse_price(offer["priceSpecification"].get("price"))
    if price is not None:
        data["price"] = price

    currency = clean_text(offer.get("priceCurrency"))
    if currency:
        data["currency"] = currency

    availability, available = availability_from_text(offer.get("availability"))
    if availability:
        data["availability"] = availability
    if available is not None:
        data["available"] = available
    return data


def jsonld_product_metadata(html: str, product_url: Optional[str] = None) -> Dict[str, Any]:
    product = _select_jsonld_product(_jsonld_candidates(html), product_url)
    if not product:
        return {}

    data: Dict[str, Any] = {}
    mapping = {
        "name": "title",
        "description": "description",
        "sku": "reference",
        "model": "reference",
        "productID": "reference",
    }
    for source, dest in mapping.items():
        value = clean_text(product.get(source))
        if value and dest not in data:
            data[dest] = value

    brand = _brand_from_json(product.get("brand"))
    if brand:
        data["brand"] = brand

    for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14", "ean"):
        gtin = normalize_gtin(product.get(key))
        if gtin:
            data["barcode"] = gtin
            break

    mpn = clean_text(product.get("mpn"))
    if mpn:
        gtin = normalize_gtin(mpn)
        if gtin:
            data.setdefault("barcode", gtin)
        else:
            data.setdefault("reference", mpn)

    images = _images_from_json(product.get("image"))
    if images:
        data["images"] = images
        data["image"] = images[0]

    data.update({k: v for k, v in _offer_metadata(product.get("offers")).items() if v is not None})
    return data


def html_product_metadata(html: str, product_url: Optional[str] = None, base_url: str = "") -> Dict[str, Any]:
    """Extract common product metadata from JSON-LD, meta tags, labels, and URLs."""
    tree = HTMLParser(html)
    data = jsonld_product_metadata(html, product_url)

    title = first_attr(tree, ["meta[itemprop='name'][content]", "meta[property='og:title'][content]"], ["content"])
    if title:
        data.setdefault("title", title)
    description = first_attr(
        tree,
        ["meta[itemprop='description'][content]", "meta[name='description'][content]", "meta[property='og:description'][content]"],
        ["content"],
    )
    if description:
        data.setdefault("description", description)
    brand = first_attr(tree, ["meta[itemprop='brand'][content]"], ["content"])
    if brand:
        data.setdefault("brand", brand)

    price = parse_price(first_attr(tree, ["meta[itemprop='price'][content]"], ["content"]))
    if price is not None:
        data.setdefault("price", price)

    availability = first_attr(tree, ["link[itemprop='availability'][href]", "meta[itemprop='availability'][content]"], ["href", "content"])
    avail_text, available = availability_from_text(availability)
    if avail_text:
        data.setdefault("availability", avail_text)
    if available is not None:
        data.setdefault("available", available)

    for selector in (
        "meta[itemprop='gtin'][content]",
        "meta[itemprop='gtin8'][content]",
        "meta[itemprop='gtin12'][content]",
        "meta[itemprop='gtin13'][content]",
        "meta[itemprop='gtin14'][content]",
        "meta[itemprop='ean'][content]",
        ".ean_wrapper .ean",
        ".sku_wrapper.ean_wrapper .ean",
        ".product-mpn span",
    ):
        node = tree.css_first(selector)
        if not node:
            continue
        value = node.attributes.get("content") or node.text(strip=True)
        gtin = normalize_gtin(value)
        if gtin:
            data.setdefault("barcode", gtin)
            break

    def apply_labeled_identifier(label: Any, value: Any) -> None:
        label_text = clean_text(label) or ""
        value_text = clean_text(value)
        if not value_text:
            return

        if re.search(r"ean|gtin|code\s*bar|barcode", label_text, re.I):
            data.setdefault("barcode", value_text)
            return

        if re.search(r"r[e\u00e9]f(?:[e\u00e9]rence)?|reference|sku|mpn|model", label_text, re.I):
            if normalize_gtin(value_text):
                data.setdefault("barcode", value_text)
            else:
                data.setdefault("reference", value_text)

    for row in tree.css("tr"):
        key_node = row.css_first("th, td:first-child")
        value_node = row.css_first("td:last-child")
        if key_node and value_node and key_node != value_node:
            apply_labeled_identifier(key_node.text(strip=True), value_node.text(strip=True))

    for block in tree.css("dl"):
        labels = block.css("dt")
        values = block.css("dd")
        for label_node, value_node in zip(labels, values):
            apply_labeled_identifier(label_node.text(strip=True), value_node.text(strip=True))

    reference = first_attr(tree, ["meta[itemprop='sku'][content]", "meta[itemprop='productID'][content]"], ["content"])
    if reference:
        data.setdefault("reference", reference)

    product_id = first_attr(
        tree,
        [
            "input[name='product_id'][value]",
            "input[name='id_product'][value]",
            "#product-id[value]",
            "[data-product-id]",
            "[data-product_id]",
        ],
        ["value", "data-product-id", "data-product_id"],
    )
    if product_id:
        data.setdefault("product_id", product_id)

    url = product_url or first_attr(tree, ["link[rel='canonical'][href]", "meta[property='og:url'][content]"], ["href", "content"])
    if url:
        data.setdefault("url", absolute_url(url, base_url or url) or url)
        path_id = re.search(r"/a/(\d+)(?:/|$)", url) or re.search(r"[?&](?:id_product|product_id|id)=(\d+)", url)
        if path_id:
            data.setdefault("product_id", path_id.group(1))

    image = first_attr(tree, ["meta[itemprop='image'][content]", "meta[property='og:image'][content]"], ["content"])
    if image:
        abs_image = absolute_url(image, base_url or product_url or "")
        if abs_image:
            data.setdefault("image", abs_image)
            data.setdefault("images", [abs_image])

    return finalize_product_record(data)


def _same_token(a: Any, b: Any) -> bool:
    left = re.sub(r"[^a-z0-9]", "", str(a or "").lower())
    right = re.sub(r"[^a-z0-9]", "", str(b or "").lower())
    return bool(left and right and left == right)


def finalize_product_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Clean identifiers so barcode/reference/sku do not contradict each other."""
    out = dict(record)

    for key in ("barcode", "reference", "sku", "brand", "title", "name", "product_id", "id"):
        if key in out:
            out[key] = clean_text(out.get(key))

    barcode = normalize_gtin(out.get("barcode"))
    invalid_barcode = clean_text(out.get("barcode")) if out.get("barcode") and not barcode else None

    for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14", "ean", "mpn"):
        if not barcode:
            barcode = normalize_gtin(out.get(key))

    reference = clean_text(out.get("reference"))
    reference_gtin = normalize_gtin(reference)
    if reference_gtin:
        barcode = barcode or reference_gtin
        reference = None

    sku = clean_text(out.get("sku"))
    sku_gtin = normalize_gtin(sku)
    if sku_gtin:
        barcode = barcode or sku_gtin
        sku = None

    brand = clean_text(out.get("brand"))
    if reference and brand and _same_token(reference, brand):
        reference = None

    if barcode:
        out["barcode"] = barcode
    else:
        out.pop("barcode", None)
        if invalid_barcode:
            out.setdefault("data_quality", {})["invalid_barcode"] = invalid_barcode

    if reference:
        out["reference"] = reference
    else:
        out.pop("reference", None)

    if not sku:
        sku = reference or barcode
    if sku:
        out["sku"] = sku
    else:
        out.pop("sku", None)

    for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14", "ean", "mpn"):
        out.pop(key, None)

    return out


def dedupe_products(
    products: List[Dict[str, Any]],
    logger: Optional[logging.Logger] = None,
    context: str = "",
) -> List[Dict[str, Any]]:
    """Drop obvious duplicates using barcode, URL, then ID."""
    seen = set()
    deduped = []
    duplicates = 0

    for product in products:
        key = None
        if product.get("barcode"):
            key = ("barcode", product["barcode"])
        elif product.get("url"):
            key = ("url", normalize_url(product["url"]))
        elif product.get("id"):
            key = ("id", str(product["id"]))

        if key and key in seen:
            duplicates += 1
            continue
        if key:
            seen.add(key)
        deduped.append(product)

    if duplicates and logger:
        label = f" {context}" if context else ""
        logger.info(f"[data_quality.duplicates]{label} removed={duplicates}")
    return deduped
