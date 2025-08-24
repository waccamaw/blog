#!/usr/bin/env python3
"""
Fetch a list of Wix blog articles from a site.

Strategy:
- Try RSS: <BASE_URL>/blog-feed.xml for rich metadata (title, pubDate, categories).
- Also read sitemap: <BASE_URL>/blog-posts-sitemap.xml for complete coverage of URLs.
- Merge results; optionally hydrate missing titles with --hydrate by scraping og:title.

Outputs: json (default), csv, or a simple table.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional


def fetch(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def guess_base_url(raw: str) -> str:
    # Ensure scheme and no trailing slash
    if not re.match(r"^https?://", raw):
        raw = "https://" + raw
    return raw.rstrip("/")


def parse_rss(base_url: str) -> Dict[str, Dict[str, Any]]:
    url = f"{base_url}/blog-feed.xml"
    items: Dict[str, Dict[str, Any]] = {}
    try:
        data = fetch(url)
    except Exception:
        return items

    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return items

    # RSS 2.0: <rss><channel><item>...
    channel = root.find("channel")
    if channel is None:
        # Sometimes namespaces are used; try wildcard search
        channel = root.find(".//{*}channel")
    if channel is None:
        return items

    for it in channel.findall("item") + channel.findall("{*}item"):
        link_el = it.find("link") or it.find("{*}link")
        link = (link_el.text or "").strip() if link_el is not None else ""
        if not link:
            continue

        title_el = it.find("title") or it.find("{*}title")
        title = html.unescape((title_el.text or "").strip()) if title_el is not None else ""

        pub_el = it.find("pubDate") or it.find("{*}pubDate")
        pub = (pub_el.text or "").strip() if pub_el is not None else ""

        cats = [
            (c.text or "").strip()
            for c in it.findall("category") + it.findall("{*}category")
            if (c.text or "").strip()
        ]

        items[link] = {
            "url": link,
            "title": title,
            "published": pub,
            "categories": cats,
            "source": "rss",
        }
    return items


def parse_blog_sitemap(base_url: str) -> List[Dict[str, Any]]:
    url = f"{base_url}/blog-posts-sitemap.xml"
    try:
        data = fetch(url)
    except Exception:
        return []

    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    out: List[Dict[str, Any]] = []
    for url_el in root.findall("sm:url", ns):
        loc_el = url_el.find("sm:loc", ns)
        lastmod_el = url_el.find("sm:lastmod", ns)
        loc = (loc_el.text or "").strip() if loc_el is not None else ""
        lastmod = (lastmod_el.text or "").strip() if lastmod_el is not None else ""
        if loc:
            out.append({"url": loc, "lastmod": lastmod})
    # Fallback: if no namespaced elements found, try without
    if not out:
        for url_el in root.findall("url"):
            loc_el = url_el.find("loc")
            lastmod_el = url_el.find("lastmod")
            loc = (loc_el.text or "").strip() if loc_el is not None else ""
            lastmod = (lastmod_el.text or "").strip() if lastmod_el is not None else ""
            if loc:
                out.append({"url": loc, "lastmod": lastmod})
    return out


def hydrate_title(url: str, timeout: float = 15.0) -> Optional[str]:
    try:
        html_bytes = fetch(url, timeout=timeout)
    except Exception:
        return None
    text = html_bytes.decode("utf-8", errors="ignore")
    # Try og:title first
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', text, re.I)
    if m:
        return html.unescape(m.group(1)).strip()
    # Fallback to <title>
    m = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
    if m:
        return html.unescape(m.group(1).strip())
    return None


def merge_posts(rss_items: Dict[str, Dict[str, Any]], sitemap_urls: List[Dict[str, Any]], hydrate: bool = False, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    # Seed from sitemap for completeness
    for entry in sitemap_urls:
        url = entry.get("url", "")
        if not url:
            continue
        merged[url] = {
            "url": url,
            "title": "",
            "published": "",
            "lastmod": entry.get("lastmod", ""),
            "categories": [],
            "source": "sitemap",
        }

    # Overlay RSS metadata where available
    for url, meta in rss_items.items():
        if url in merged:
            merged[url].update({
                "title": meta.get("title", merged[url]["title"]),
                "published": meta.get("published", merged[url]["published"]),
                "categories": meta.get("categories", merged[url]["categories"]),
                "source": "rss+sitemap",
            })
        else:
            merged[url] = meta

    posts = list(merged.values())

    # Hydrate titles for posts missing a title if requested
    if hydrate:
        count = 0
        for p in posts:
            if p.get("title"):
                continue
            t = hydrate_title(p["url"])
            if t:
                p["title"] = t
                p["source"] = p.get("source", "") + "+title"
            count += 1
            # Be nice to the server
            time.sleep(0.3)
            if limit and count >= limit:
                break

    # Sort: prefer published or lastmod descending
    def sort_key(p: Dict[str, Any]):
        # Use lastmod/published string as a proxy; ISO strings sort acceptably; RSS is RFC822, but rough sort is fine.
        return (p.get("published") or p.get("lastmod") or "")

    posts.sort(key=sort_key, reverse=True)

    if limit:
        posts = posts[:limit]
    return posts


def output_posts(posts: List[Dict[str, Any]], fmt: str) -> None:
    if fmt == "json":
        json.dump(posts, sys.stdout, ensure_ascii=False, indent=2)
        print()
    elif fmt == "csv":
        fieldnames = ["title", "url", "published", "lastmod", "categories", "source"]
        w = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        w.writeheader()
        for p in posts:
            row = p.copy()
            row["categories"] = ",".join(row.get("categories", []) or [])
            w.writerow({k: row.get(k, "") for k in fieldnames})
    else:  # table
        # Simple fixed-width columns
        def trunc(s: str, n: int) -> str:
            return (s[: n - 1] + "…") if len(s) > n else s
        print(f"{'#':>3}  {'Title':<60}  {'Published/Lastmod':<29}  URL")
        print("-" * 120)
        for i, p in enumerate(posts, 1):
            when = p.get("published") or p.get("lastmod") or ""
            title = p.get("title") or "(no title)"
            print(f"{i:>3}  {trunc(title, 60):<60}  {trunc(when, 29):<29}  {p.get('url','')}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch Wix blog posts list")
    ap.add_argument("--base-url", default=os.getenv("SOURCE_URL"), help="Base site URL, e.g. https://example.com (default: $SOURCE_URL)")
    ap.add_argument("--format", choices=["json", "csv", "table"], default="table", help="Output format")
    ap.add_argument("--hydrate", action="store_true", help="Fetch pages to fill missing titles (slower)")
    ap.add_argument("--limit", type=int, default=None, help="Limit number of posts")
    args = ap.parse_args(argv)

    if not args.base_url:
        print("Error: --base-url or SOURCE_URL is required", file=sys.stderr)
        return 2

    base = guess_base_url(args.base_url)

    rss_items = parse_rss(base)
    sitemap_urls = parse_blog_sitemap(base)

    if not rss_items and not sitemap_urls:
        print("Error: Could not fetch RSS or sitemap from base URL", file=sys.stderr)
        return 1

    posts = merge_posts(rss_items, sitemap_urls, hydrate=args.hydrate, limit=args.limit)
    output_posts(posts, args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
