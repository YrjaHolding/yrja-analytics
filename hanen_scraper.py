"""Scrape HANEN producer pages into structured product intelligence.

The default target is HANEN's meat-producer category:

    https://www.hanen.no/opplev-norge/mat-og-drikkeprodusenter/?_hanen_category=kjottprodusenter

The scraper crawls the paginated listing, visits each ``/bedrift/<slug>/`` page,
and extracts the producer's descriptions, carousel captions, contact details,
and rule-based product/category signals.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

DEFAULT_START_URL = (
    "https://www.hanen.no/opplev-norge/mat-og-drikkeprodusenter/"
    "?_hanen_category=kjottprodusenter"
)

HANEN_HOST = "www.hanen.no"
LISTING_PATH_RE = re.compile(
    r"^/opplev-norge/mat-og-drikkeprodusenter/(?:page/\d+/?)?$"
)
BUSINESS_PATH_RE = re.compile(r"^/bedrift/([^/]+)/?$")

PRODUCT_CATEGORY_TERMS: dict[str, list[str]] = {
    "meat": [
        "and",
        "bacon",
        "biff",
        "burger",
        "elg",
        "fenalår",
        "geit",
        "gris",
        "hjort",
        "høylandsfe",
        "karbonade",
        "killing",
        "kjøtt",
        "kjøttdeig",
        "kylling",
        "lam",
        "lammekjøtt",
        "landsvin",
        "oksekjøtt",
        "pinnekjøtt",
        "pølse",
        "ribbe",
        "rein",
        "sau",
        "slakt",
        "slakteri",
        "spekemat",
        "spekeskinke",
        "storfe",
        "storfekjøtt",
        "svin",
        "ullgris",
        "utegangergris",
        "utegris",
        "vilt",
    ],
    "dairy_and_eggs": [
        "blåmuggost",
        "brunost",
        "chevre",
        "egg",
        "geitost",
        "halloumi",
        "hvitmuggost",
        "iskrem",
        "melk",
        "ost",
        "oster",
        "rømme",
        "smør",
        "yoghurt",
        "ysteri",
    ],
    "fruit_vegetables_and_potatoes": [
        "agurk",
        "beter",
        "bringebær",
        "bær",
        "eple",
        "gulløye",
        "gulrot",
        "jordbær",
        "kål",
        "løk",
        "multe",
        "nypotet",
        "plomme",
        "potet",
        "poteter",
        "rips",
        "rotgrønnsaker",
        "salat",
        "solbær",
        "tomat",
        "van gogh",
        "grønnsak",
        "grønnsaker",
    ],
    "fish_and_seafood": [
        "fisk",
        "klippfisk",
        "krabbe",
        "laks",
        "lofoten seaweed",
        "reke",
        "skalldyr",
        "sjømat",
        "tang",
        "tare",
        "torsk",
        "ørret",
    ],
    "bakery_and_grains": [
        "bakeri",
        "bakst",
        "brød",
        "flatbrød",
        "kake",
        "knekkebrød",
        "korn",
        "lefse",
        "mel",
    ],
    "drinks": [
        "akevitt",
        "brenneri",
        "bryggeri",
        "cider",
        "gin",
        "juice",
        "jus",
        "kaffe",
        "most",
        "saft",
        "sirup",
        "te",
        "vin",
        "øl",
    ],
    "preserves_condiments_and_honey": [
        "chutney",
        "eddik",
        "gelé",
        "honning",
        "krydder",
        "marmelade",
        "olje",
        "salt",
        "saus",
        "sylte",
        "syltetøy",
        "urter",
    ],
    "prepared_foods_and_sales_channels": [
        "abonnement",
        "bondens marked",
        "catering",
        "delikatesse",
        "gave",
        "gårdsbutikk",
        "lokalmat",
        "lunsj",
        "middag",
        "nettbutikk",
        "reko-ringen",
        "restaurant",
        "servering",
        "utsalg",
    ],
}

FOOTER_TEXT_MARKERS = (
    "besøksadresse: hollendergata",
    "hanen har ikke ansvar",
    "innholdet på disse sidene",
    "kontakt hanen",
    "meld deg på vårt nyhetsbrev",
    "postadresse: postboks",
    "personvernserklæring",
    "tredjeparts lisenser",
)

LABELS = {
    "kontaktperson": "contact_person",
    "adresse": "address",
    "telefon": "phone",
    "nettside": "website",
    "e-post": "email",
}

SOCIAL_HOST_PARTS = (
    "facebook.",
    "instagram.",
    "linkedin.",
    "tiktok.",
    "youtube.",
    "x.com",
    "twitter.",
)

EXTERNAL_SKIP_HOST_PARTS = (
    "cloudinary.com",
    "gmpg.org",
    "google.",
    "googleapis.",
    "gstatic.",
    "gravatar.",
    "hanen.us5.list-manage.com",
    "w.org",
    "wp.org",
)

EXTERNAL_PAGE_HINTS = (
    "produkt",
    "produkter",
    "butikk",
    "nettbutikk",
    "utsalg",
    "shop",
    "varer",
    "kjøtt",
    "kjott",
    "ost",
    "mat",
    "meny",
    "gårdsbutikk",
    "gardsbutikk",
)


@dataclass
class FetchResult:
    url: str
    html: str


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def text_from_node(node: Any, separator: str = " ") -> str:
    if node is None:
        return ""
    return clean_text(node.get_text(separator, strip=True))


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = re.sub(r"\s+", " ", value).strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def same_host(url: str, expected_host: str = HANEN_HOST) -> bool:
    host = urlsplit(url).netloc.lower()
    return host == expected_host or host == expected_host.removeprefix("www.")


def infer_category_filter(start_url: str) -> str | None:
    values = parse_qs(urlsplit(start_url).query).get("_hanen_category")
    return values[0] if values else None


def is_listing_url(url: str, category_filter: str | None) -> bool:
    parts = urlsplit(url)
    if not same_host(url):
        return False
    if not LISTING_PATH_RE.match(parts.path):
        return False
    if category_filter is None:
        return True
    return category_filter in parse_qs(parts.query).get("_hanen_category", [])


def is_business_url(url: str) -> bool:
    parts = urlsplit(url)
    return same_host(url) and bool(BUSINESS_PATH_RE.match(parts.path))


def business_slug(url: str) -> str:
    match = BUSINESS_PATH_RE.match(urlsplit(url).path)
    return match.group(1) if match else ""


def normalize_label(text: str) -> str:
    return clean_text(text).rstrip(":").casefold()


def is_social_url(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(part in host for part in SOCIAL_HOST_PARTS)


def is_skipped_external_url(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(part in host for part in EXTERNAL_SKIP_HOST_PARTS)


def should_keep_description_block(text: str) -> bool:
    if len(text) < 80:
        return False
    lower = text.casefold()
    if any(marker in lower for marker in FOOTER_TEXT_MARKERS):
        return False
    if normalize_label(text) in LABELS:
        return False
    return True


# Norwegian builds nouns by compounding (e.g. "storfekjøtt", "gårdsbakeri") and
# inflection ("kjøttet", "pølser"). These distinctive roots are matched as
# substrings so compounds/inflections are caught; they are specific enough that
# coincidental substring hits are very unlikely. Every other term keeps word
# boundaries so short/ambiguous words stay safe (e.g. "vin" never hits "vinter",
# "ost" never hits "post").
COMPOUND_SAFE_TERMS = frozenset(
    {
        "kjøtt",
        "pølse",
        "bakeri",
        "bakst",
        "potet",
        "fisk",
        "grønnsak",
        "honning",
        "marmelade",
        "syltetøy",
        "bryggeri",
        "brenneri",
        "slakteri",
        "ysteri",
        "spekemat",
        "spekeskinke",
        "pinnekjøtt",
        "lammekjøtt",
        "oksekjøtt",
        "storfekjøtt",
        "fenalår",
        "høylandsfe",
        "flatbrød",
        "knekkebrød",
        "klippfisk",
        "skalldyr",
        "bringebær",
        "jordbær",
        "solbær",
        "krydder",
        "delikatesse",
        "gårdsbutikk",
        "nettbutikk",
        "lokalmat",
        "abonnement",
    }
)


def product_term_pattern(term: str) -> re.Pattern[str]:
    if term in COMPOUND_SAFE_TERMS:
        return re.compile(re.escape(term), re.IGNORECASE)
    return re.compile(rf"(?<![\wæøå]){re.escape(term)}(?![\wæøå])", re.IGNORECASE)


def analyze_products(text: str, max_snippets: int = 10) -> dict[str, Any]:
    category_scores: dict[str, int] = {}
    matched_terms_by_category: dict[str, list[str]] = {}

    for category, terms in PRODUCT_CATEGORY_TERMS.items():
        matched_terms: list[str] = []
        score = 0
        for term in terms:
            matches = product_term_pattern(term).findall(text)
            if matches:
                matched_terms.append(term)
                score += len(matches)
        if matched_terms:
            category_scores[category] = score
            matched_terms_by_category[category] = matched_terms

    inferred_categories = [
        category
        for category, _ in sorted(
            category_scores.items(), key=lambda item: item[1], reverse=True
        )
    ]
    all_terms = sorted(
        {term for terms in matched_terms_by_category.values() for term in terms}
    )

    return {
        "inferred_product_categories": inferred_categories,
        "product_category_scores": category_scores,
        "matched_product_terms": matched_terms_by_category,
        "evidence_snippets": evidence_snippets(text, all_terms, max_snippets),
    }


def evidence_snippets(text: str, terms: list[str], max_snippets: int) -> list[str]:
    if not text or not terms:
        return []

    term_patterns = [product_term_pattern(term) for term in terms]
    snippets: list[str] = []
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)

    for sentence in sentences:
        sentence = clean_text(sentence)
        if len(sentence) < 30:
            continue
        if any(pattern.search(sentence) for pattern in term_patterns):
            snippets.append(sentence[:500])
        if len(snippets) >= max_snippets:
            break

    return dedupe_keep_order(snippets)


class HanenScraper:
    def __init__(self, *, delay_seconds: float = 0.75, retries: int = 3) -> None:
        self.delay_seconds = delay_seconds
        self.retries = retries
        self._last_request_at = 0.0
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "nb,no;q=0.9,en;q=0.7",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 HanenProducerScraper/1.0"
                ),
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HanenScraper:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def fetch_html(self, url: str) -> FetchResult:
        wait_for = self.delay_seconds - (time.monotonic() - self._last_request_at)
        if wait_for > 0:
            time.sleep(wait_for)

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                log.debug("GET %s", url)
                response = self._client.get(url)
                self._last_request_at = time.monotonic()
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "html" not in content_type.lower():
                    raise ValueError(f"Expected HTML, got {content_type!r}")
                return FetchResult(url=str(response.url), html=response.text)
            except Exception as exc:  # noqa: BLE001 - retry all transient fetch issues
                last_error = exc
                if attempt == self.retries:
                    break
                sleep_for = min(2**attempt, 8)
                log.warning("Fetch failed for %s (%s), retrying in %ss", url, exc, sleep_for)
                time.sleep(sleep_for)

        assert last_error is not None
        raise last_error

    def discover_producer_urls(
        self,
        start_url: str,
        *,
        max_listing_pages: int | None = None,
    ) -> list[str]:
        category_filter = infer_category_filter(start_url)
        queue: deque[str] = deque([canonical_url(start_url)])
        seen_listing_pages: set[str] = set()
        producer_urls: list[str] = []
        seen_producers: set[str] = set()

        while queue:
            listing_url = queue.popleft()
            if listing_url in seen_listing_pages:
                continue
            if max_listing_pages is not None and len(seen_listing_pages) >= max_listing_pages:
                break

            seen_listing_pages.add(listing_url)
            log.info("Scraping listing page %s", listing_url)
            fetch = self.fetch_html(listing_url)
            soup = BeautifulSoup(fetch.html, "html.parser")

            for anchor in soup.find_all("a", href=True):
                href = canonical_url(urljoin(fetch.url, anchor["href"]))
                if is_business_url(href) and href not in seen_producers:
                    seen_producers.add(href)
                    producer_urls.append(href)
                elif is_listing_url(href, category_filter) and href not in seen_listing_pages:
                    queue.append(href)

        log.info(
            "Discovered %s producer URLs across %s listing pages",
            len(producer_urls),
            len(seen_listing_pages),
        )
        return producer_urls

    def scrape(
        self,
        start_url: str,
        *,
        max_listing_pages: int | None = None,
        max_producers: int | None = None,
        scrape_websites: bool = False,
        external_pages_per_producer: int = 3,
    ) -> list[dict[str, Any]]:
        producer_urls = self.discover_producer_urls(
            start_url, max_listing_pages=max_listing_pages
        )
        if max_producers is not None:
            producer_urls = producer_urls[:max_producers]

        producers: list[dict[str, Any]] = []
        for index, producer_url in enumerate(producer_urls, start=1):
            log.info("Scraping producer %s/%s: %s", index, len(producer_urls), producer_url)
            try:
                fetch = self.fetch_html(producer_url)
                producer = parse_producer_page(fetch.html, fetch.url)
                if scrape_websites and producer.get("website"):
                    external = self.scrape_external_website(
                        producer["website"],
                        max_pages=external_pages_per_producer,
                    )
                    producer["external_pages_scraped"] = external["pages"]
                    producer["external_text"] = external["text"]
                    combined_text = "\n\n".join(
                        [
                            producer.get("product_evidence_text", ""),
                            external["text"],
                        ]
                    ).strip()
                    producer.update(analyze_products(combined_text))
                producers.append(producer)
            except Exception as exc:  # noqa: BLE001 - keep batch scrape moving
                log.exception("Failed to scrape %s", producer_url)
                producers.append(
                    {
                        "url": producer_url,
                        "slug": business_slug(producer_url),
                        "error": str(exc),
                        "scraped_at": datetime.now(UTC).isoformat(),
                    }
                )

        return producers

    def scrape_external_website(self, start_url: str, *, max_pages: int) -> dict[str, Any]:
        queue: deque[str] = deque([canonical_url(start_url)])
        seen: set[str] = set()
        scraped_pages: list[str] = []
        texts: list[str] = []
        start_host = urlsplit(start_url).netloc.lower().removeprefix("www.")

        while queue and len(scraped_pages) < max_pages:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            try:
                fetch = self.fetch_html(url)
            except Exception as exc:  # noqa: BLE001
                log.debug("Skipping external URL %s: %s", url, exc)
                continue

            scraped_pages.append(fetch.url)
            soup = BeautifulSoup(fetch.html, "html.parser")
            page_text = visible_page_text(soup)
            if page_text:
                texts.append(page_text[:10_000])

            candidates = []
            for anchor in soup.find_all("a", href=True):
                href = canonical_url(urljoin(fetch.url, anchor["href"]))
                parts = urlsplit(href)
                host = parts.netloc.lower().removeprefix("www.")
                if parts.scheme not in {"http", "https"} or host != start_host:
                    continue
                if href in seen:
                    continue
                if re.search(r"\.(pdf|jpg|jpeg|png|webp|gif|zip)(?:$|\?)", parts.path):
                    continue

                link_text = text_from_node(anchor)
                score = external_link_score(href, link_text)
                if score:
                    candidates.append((score, href))

            for _, href in sorted(candidates, reverse=True):
                if href not in seen:
                    queue.append(href)

        return {
            "pages": scraped_pages,
            "text": "\n\n".join(dedupe_keep_order(texts)),
        }


def parse_producer_page(html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    name = extract_name(soup)
    categories = extract_member_categories(soup)
    contact_fields = extract_labeled_contact_fields(soup)
    website = extract_member_website(soup) or contact_fields.get("website", "")
    email = extract_member_email(soup) or contact_fields.get("email", "")
    phone = extract_member_phone(soup) or contact_fields.get("phone", "")
    description_blocks = extract_description_blocks(soup)
    captions = extract_carousel_captions(soup)
    image_alts = extract_descriptive_image_alts(soup, captions)
    product_evidence_text = "\n\n".join(
        dedupe_keep_order(description_blocks + captions + image_alts)
    )

    producer: dict[str, Any] = {
        "name": name,
        "url": url,
        "slug": business_slug(url),
        "scraped_at": datetime.now(UTC).isoformat(),
        "hanen_categories": categories,
        "contact_person": contact_fields.get("contact_person", ""),
        "address": contact_fields.get("address", ""),
        "phone": phone,
        "email": email,
        "website": website,
        "social_links": extract_social_links(soup),
        "external_memberships": extract_external_memberships(soup),
        "description_blocks": description_blocks,
        "carousel_captions": captions,
        "descriptive_image_alts": image_alts,
        "product_evidence_text": product_evidence_text,
    }
    producer.update(analyze_products(product_evidence_text))
    return producer


def extract_name(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        name = text_from_node(h1)
        if name:
            return name

    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        return clean_title(og_title["content"])

    if soup.title and soup.title.string:
        return clean_title(soup.title.string)

    return ""


def clean_title(title: str) -> str:
    return re.sub(r"\s+-\s+HANEN\s*$", "", clean_text(title), flags=re.IGNORECASE)


def extract_member_categories(soup: BeautifulSoup) -> list[str]:
    categories = []
    for img in soup.select(".hanen-cat-icon-img[alt]"):
        alt = clean_text(img["alt"])
        if alt:
            categories.append(alt)
    for anchor in soup.select(".hanen-cat-icon-link[title]"):
        title = clean_text(anchor["title"])
        if title:
            categories.append(title)
    return dedupe_keep_order(categories)


def extract_labeled_contact_fields(soup: BeautifulSoup) -> dict[str, str]:
    fields: dict[str, str] = {}
    for rich_text in soup.select(".fl-rich-text"):
        label = normalize_label(text_from_node(rich_text))
        if label not in LABELS:
            continue
        group = rich_text.find_parent(class_="fl-col-group-nested")
        if not group:
            continue
        values = []
        for candidate in group.select(".fl-rich-text"):
            value = text_from_node(candidate)
            if normalize_label(value) == label:
                continue
            if value:
                values.append(value)
        if values:
            fields[LABELS[label]] = " ".join(values)
    return fields


def extract_member_website(soup: BeautifulSoup) -> str:
    link = soup.select_one("a.member-website-link[href]")
    if link:
        return canonical_url(link["href"])

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not href.startswith(("http://", "https://")):
            continue
        if same_host(href) or is_social_url(href) or is_skipped_external_url(href):
            continue
        return canonical_url(href)
    return ""


def extract_member_email(soup: BeautifulSoup) -> str:
    for anchor in soup.select('a[href^="mailto:"]'):
        email = anchor["href"].removeprefix("mailto:").split("?", 1)[0].strip()
        if email and email.casefold() != "post@hanen.no":
            return email
    return ""


def extract_member_phone(soup: BeautifulSoup) -> str:
    for anchor in soup.select('a[href^="tel:"]'):
        phone = text_from_node(anchor)
        if phone:
            return phone
        return anchor["href"].removeprefix("tel:").strip()
    return ""


def extract_social_links(soup: BeautifulSoup) -> dict[str, str]:
    links: dict[str, str] = {}
    for anchor in soup.select(".member-social-icons a[href]"):
        href = canonical_url(anchor["href"])
        host = urlsplit(href).netloc.lower()
        platform = host.removeprefix("www.").split(".", 1)[0]
        links[platform] = href
    return links


def extract_external_memberships(soup: BeautifulSoup) -> list[dict[str, str]]:
    memberships = []
    for anchor in soup.select(".external-memberships a[href]"):
        img = anchor.find("img")
        name = clean_text(img.get("alt", "")) if img else text_from_node(anchor)
        memberships.append(
            {
                "name": name,
                "url": canonical_url(urljoin("https://www.hanen.no/", anchor["href"])),
            }
        )
    return memberships


def extract_description_blocks(soup: BeautifulSoup) -> list[str]:
    blocks = []
    for rich_text in soup.select(".fl-rich-text"):
        text = text_from_node(rich_text, separator="\n")
        if should_keep_description_block(text):
            blocks.append(text)
    return dedupe_keep_order(blocks)


def extract_carousel_captions(soup: BeautifulSoup) -> list[str]:
    captions = []
    for caption in soup.select(".sf-caption"):
        text = text_from_node(caption, separator="\n")
        if len(text) >= 40:
            captions.append(text)
    return dedupe_keep_order(captions)


def extract_descriptive_image_alts(
    soup: BeautifulSoup, existing_texts: list[str]
) -> list[str]:
    existing = "\n\n".join(existing_texts).casefold()
    alts = []
    for img in soup.find_all("img", alt=True):
        alt = clean_text(img["alt"])
        if len(alt) < 80:
            continue
        if any(marker in alt.casefold() for marker in FOOTER_TEXT_MARKERS):
            continue
        if alt.casefold() in existing:
            continue
        alts.append(alt)
    return dedupe_keep_order(alts)


def visible_page_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "form"]):
        tag.decompose()
    container = soup.find("main") or soup.body or soup
    parts = []
    for tag in container.find_all(["h1", "h2", "h3", "p", "li"]):
        text = text_from_node(tag)
        if len(text) >= 25:
            parts.append(text)
    return "\n".join(dedupe_keep_order(parts))


def external_link_score(url: str, link_text: str) -> int:
    haystack = f"{url} {link_text}".casefold()
    score = sum(1 for hint in EXTERNAL_PAGE_HINTS if hint in haystack)
    if urlsplit(url).path in {"", "/"}:
        score += 1
    return score


def write_json(path: Path, producers: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(producers, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_csv(path: Path, producers: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "name",
        "url",
        "slug",
        "hanen_categories",
        "inferred_product_categories",
        "matched_product_terms",
        "address",
        "phone",
        "email",
        "website",
        "contact_person",
        "evidence_snippets",
        "error",
    ]

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for producer in producers:
            writer.writerow(
                {
                    key: flatten_csv_value(producer.get(key, ""))
                    for key in fieldnames
                }
            )


def flatten_csv_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(
            item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            for item in value
        )
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return str(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Deep scrape HANEN producer listing pages and infer what products "
            "each producer makes/sells."
        )
    )
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument(
        "--output-json",
        default="hanen_producers_kjottprodusenter.json",
        help="Path for JSON output. Use an empty string to skip JSON.",
    )
    parser.add_argument(
        "--output-csv",
        default="hanen_producers_kjottprodusenter.csv",
        help="Path for CSV output. Use an empty string to skip CSV.",
    )
    parser.add_argument("--delay", type=float, default=0.75, help="Delay between requests.")
    parser.add_argument("--max-listing-pages", type=int, default=None)
    parser.add_argument("--max-producers", type=int, default=None)
    parser.add_argument(
        "--scrape-websites",
        action="store_true",
        help=(
            "Also crawl each producer's own website, same-domain only, up to "
            "--external-pages-per-producer pages. Disabled by default."
        ),
    )
    parser.add_argument("--external-pages-per-producer", type=int, default=3)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(message)s",
    )

    with HanenScraper(delay_seconds=args.delay) as scraper:
        producers = scraper.scrape(
            args.start_url,
            max_listing_pages=args.max_listing_pages,
            max_producers=args.max_producers,
            scrape_websites=args.scrape_websites,
            external_pages_per_producer=args.external_pages_per_producer,
        )

    if args.output_json:
        write_json(Path(args.output_json), producers)
        log.info("Wrote JSON: %s", args.output_json)
    if args.output_csv:
        write_csv(Path(args.output_csv), producers)
        log.info("Wrote CSV: %s", args.output_csv)

    errors = sum(1 for producer in producers if producer.get("error"))
    log.info("Finished %s producers (%s errors)", len(producers), errors)


if __name__ == "__main__":
    main()
