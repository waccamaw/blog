#!/usr/bin/env python3
"""
Export Wix blog posts to local Markdown files with YAML front matter.

- Reads base URL from --base-url or $SOURCE_URL
- Uses /blog-feed.xml for metadata + content (content:encoded) when available
- Uses /blog-posts-sitemap.xml to ensure full coverage
- Writes to an output directory, organized by year/slug with index.md
  Example: content/posts/2025/autumn-equinox-2025/index.md

Notes:
- Inline images in the HTML body are left as remote URLs (safe default).
- If an RSS enclosure (hero image) exists, it's downloaded as featured.* and referenced in front matter.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from html.parser import HTMLParser

# Allow importing sibling module if run from repo root
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from fetch_wix_blog import guess_base_url, parse_blog_sitemap
except Exception:
    # Fallback minimal implementations
    def guess_base_url(raw: str) -> str:
        if not re.match(r"^https?://", raw):
            raw = "https://" + raw
        return raw.rstrip("/")

    def parse_blog_sitemap(base_url: str) -> List[Dict[str, Any]]:
        return []


UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"


def http_get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_rss_full(base_url: str) -> Dict[str, Dict[str, Any]]:
    url = f"{base_url}/blog-feed.xml"
    try:
        raw = http_get(url)
    except Exception:
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    ns = {
        "content": "http://purl.org/rss/1.0/modules/content/",
        "dc": "http://purl.org/dc/elements/1.1/",
        "atom": "http://www.w3.org/2005/Atom",
    }
    channel = root.find("channel") or root.find(".//{*}channel")
    if channel is None:
        return {}
    items: Dict[str, Dict[str, Any]] = {}
    for it in channel.findall("item") + channel.findall("{*}item"):
        def txt(tag: str, ns_key: Optional[str] = None) -> str:
            if ns_key:
                el = it.find(f"{{{ns[ns_key]}}}{tag}")
            else:
                el = it.find(tag) or it.find(f"{{*}}{tag}")
            return (el.text or "").strip() if el is not None else ""

        link = txt("link")
        if not link:
            continue
        title = html.unescape(txt("title"))
        pub = txt("pubDate")
        creator = txt("creator", "dc")
        content_html = txt("encoded", "content")
        guid = txt("guid")
        enclosure_url = ""
        for enc in it.findall("enclosure") + it.findall("{*}enclosure"):
            url_attr = enc.get("url")
            if url_attr:
                enclosure_url = url_attr
                break
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
            "author": creator,
            "guid": guid,
            "content_html": content_html,
            "enclosure": enclosure_url,
            "source": "rss",
        }
    return items


def parse_rfc822_date(s: str) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        return None


def parse_iso_date(s: str) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        # Handle YYYY-MM-DD or full ISO
        if len(s) == 10:
            return dt.datetime.fromisoformat(s + "T00:00:00+00:00")
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def path_safe_slug(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9-_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "post"


def slug_from_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    parts = [seg for seg in p.path.split("/") if seg]
    if parts:
        slug = parts[-1]
    else:
        slug = p.netloc
    return path_safe_slug(slug)


def ensure_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def download_file(url: str, dest: pathlib.Path) -> bool:
    try:
        data = http_get(url)
        dest.write_bytes(data)
        return True
    except Exception:
        return False


def ext_from_url(url: str, default: str = ".jpg") -> str:
    path = urllib.parse.urlparse(url).path
    _, ext = os.path.splitext(path)
    return ext or default


def extract_article_from_html(page_html: str) -> Optional[str]:
    # Try <article>...</article>
    m = re.search(r"<article[\s\S]*?</article>", page_html, re.I)
    if m:
        return m.group(0)
    # Try common Wix content container patterns
    m = re.search(r"<div[^>]+data-hook=[\"']post-content[\"'][\s\S]*?</div>", page_html, re.I)
    if m:
        return m.group(0)
    m = re.search(r"<div[^>]+class=[\"'][^\"']*(?:post|article)[^\"']*[\"'][\s\S]*?</div>", page_html, re.I)
    if m:
        return m.group(0)
    # Try JSON-LD articleBody as plain text wrapped in paragraphs
    for m in re.finditer(r"<script[^>]+type=\"application/ld\+json\"[^>]*>([\s\S]*?)</script>", page_html, re.I):
        try:
            import json as _json
            data = _json.loads(m.group(1))
            # Could be a list or object
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if isinstance(node, dict) and node.get("@type") in ("Article", "BlogPosting"):
                    body = node.get("articleBody")
                    if isinstance(body, str) and body.strip():
                        paras = "\n\n".join(f"<p>{html.escape(p.strip())}</p>" for p in body.split("\n") if p.strip())
                        return paras
        except Exception:
            continue
    return None


def download_inline_images(html_body: str, folder: pathlib.Path) -> Tuple[str, int]:
    ensure_dir(folder)
    count = 0
    def repl(m: re.Match) -> str:
        nonlocal count
        full = m.group(0)
        url = m.group(1)
        # Only handle http(s) images
        if not url.startswith("http://") and not url.startswith("https://"):
            return full
        # Derive filename
        base = os.path.basename(urllib.parse.urlparse(url).path) or f"image-{count}.jpg"
        # De-duplicate
        dest = folder / base
        i = 1
        while dest.exists():
            name, ext = os.path.splitext(base)
            dest = folder / f"{name}-{i}{ext}"
            i += 1
        if download_file(url, dest):
            count += 1
            return full.replace(url, f"./{folder.name}/{dest.name}")
        return full
    new_html = re.sub(r"<img[^>]+src=\"([^\"]+)\"[^>]*>", repl, html_body, flags=re.I)
    return new_html, count


class MarkdownHTMLParser(HTMLParser):
    """A minimal HTML -> Markdown converter using the stdlib HTMLParser.

    Focuses on simple, readable Markdown: headings, paragraphs, lists, links,
    bold/italic, code, blockquotes, line breaks.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.list_stack: List[str] = []  # 'ul' or 'ol'
        self.ol_counters: List[int] = []
        self.in_strong = False
        self.in_em = False
        self.in_code = False
        self.in_pre = False
        self.in_link: List[str] = []  # href stack
        self.block_pending_newlines = 0

    def _ensure_block_sep(self, lines: int = 2) -> None:
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")
        # ensure at least lines newlines at block boundaries
        if self.out:
            while lines > 0 and (len(self.out) < 2 or self.out[-1] != "\n" or self.out[-2] != "\n"):
                self.out.append("\n")
                lines -= 1
        else:
            self.out.append("\n" * lines)

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]):
        attrs = dict(attrs_list)
        tag = tag.lower()
        if tag in {"p", "div", "section", "article"}:
            self._ensure_block_sep(2)
        elif tag in {"br"}:
            self.out.append("\n")
        elif tag in {"strong", "b"}:
            self.in_strong = True
            self.out.append("**")
        elif tag in {"em", "i"}:
            self.in_em = True
            self.out.append("*")
        elif tag == "code":
            if self.in_pre:
                # inside pre, we'll fence separately
                pass
            else:
                self.in_code = True
                self.out.append("`")
        elif tag == "pre":
            self._ensure_block_sep(2)
            self.in_pre = True
            self.out.append("```\n")
        elif tag in {"ul", "ol"}:
            self._ensure_block_sep(1)
            self.list_stack.append(tag)
            if tag == "ol":
                self.ol_counters.append(1)
        elif tag == "li":
            indent = "  " * (len(self.list_stack) - 1)
            if self.list_stack and self.list_stack[-1] == "ol":
                idx = self.ol_counters[-1]
                self.out.append(f"{indent}{idx}. ")
                self.ol_counters[-1] = idx + 1
            else:
                self.out.append(f"{indent}- ")
        elif tag == "a":
            href = attrs.get("href", "") or ""
            self.in_link.append(href)
            self.out.append("[")
        elif tag == "h1":
            self._ensure_block_sep(2)
            self.out.append("# ")
        elif tag == "h2":
            self._ensure_block_sep(2)
            self.out.append("## ")
        elif tag == "h3":
            self._ensure_block_sep(2)
            self.out.append("### ")
        elif tag == "h4":
            self._ensure_block_sep(2)
            self.out.append("#### ")
        elif tag == "h5":
            self._ensure_block_sep(2)
            self.out.append("##### ")
        elif tag == "h6":
            self._ensure_block_sep(2)
            self.out.append("###### ")
        elif tag == "blockquote":
            self._ensure_block_sep(1)
            self.out.append("> ")
        elif tag == "img":
            alt = attrs.get("alt", "") or ""
            src = attrs.get("src", "") or ""
            if src:
                # Represent as a link to keep content light
                text = alt or "image"
                self.out.append(f"[{text}]({src})")

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in {"strong", "b"} and self.in_strong:
            self.out.append("**")
            self.in_strong = False
        elif tag in {"em", "i"} and self.in_em:
            self.out.append("*")
            self.in_em = False
        elif tag == "code" and self.in_code:
            self.out.append("`")
            self.in_code = False
        elif tag == "pre" and self.in_pre:
            if not self.out or not self.out[-1].endswith("\n"):
                self.out.append("\n")
            self.out.append("```\n")
            self.in_pre = False
        elif tag in {"ul"}:
            if self.list_stack:
                self.list_stack.pop()
            self._ensure_block_sep(1)
        elif tag in {"ol"}:
            if self.list_stack:
                self.list_stack.pop()
            if self.ol_counters:
                self.ol_counters.pop()
            self._ensure_block_sep(1)
        elif tag == "li":
            self.out.append("\n")
        elif tag == "a":
            # Close markdown link
            href = self.in_link.pop() if self.in_link else ""
            self.out.append("]")
            if href:
                self.out.append(f"({href})")
        elif tag in {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}:
            self.out.append("\n\n")

    def handle_data(self, data: str):
        if not data:
            return
        self.out.append(data)

    def getvalue(self) -> str:
        text = "".join(self.out)
        # Normalize excessive blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Trim whitespace around lines
        lines = [ln.rstrip() for ln in text.splitlines()]
        return "\n".join(lines).strip() + "\n"


def html_to_markdown(html_text: str) -> str:
    parser = MarkdownHTMLParser()
    parser.feed(html_text)
    return parser.getvalue()


def html_to_plain(html_text: str) -> str:
    # Remove scripts/styles
    text = re.sub(r"<script[\s\S]*?</script>", " ", html_text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    # Break on <br> and block tags
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|section|article|h\d|li|blockquote)>", "\n", text, flags=re.I)
    # Strip other tags
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    # Restore paragraph-ish breaks
    text = re.sub(r"(\n\s*)+", "\n", text)
    return text.strip() + "\n"


def write_markdown(
    post: Dict[str, Any],
    out_root: pathlib.Path,
    structure: str = "year/slug",
    download_hero: bool = True,
    scrape_if_missing: bool = False,
    download_inline: bool = False,
    body_format: str = "markdown",  # html | markdown | plain
) -> pathlib.Path:
    # Determine date and folder
    pub_dt = parse_rfc822_date(post.get("published", ""))
    lastmod_dt = parse_iso_date(post.get("lastmod", ""))
    date_dt = pub_dt or lastmod_dt or dt.datetime.now(dt.timezone.utc)
    year = f"{date_dt.year:04d}"

    slug = slug_from_url(post["url"]) if post.get("url") else path_safe_slug(post.get("title", "post"))

    if structure == "year/slug":
        folder = out_root / year / slug
    elif structure == "year/month/slug":
        folder = out_root / year / f"{date_dt.month:02d}" / slug
    elif structure == "flat":
        folder = out_root / slug
    else:
        folder = out_root / year / slug

    ensure_dir(folder)
    index_md = folder / "index.md"

    # Prepare front matter
    date_iso = date_dt.isoformat().replace("+00:00", "Z")
    lastmod_iso = (lastmod_dt.isoformat().replace("+00:00", "Z") if lastmod_dt else "")
    fm: Dict[str, Any] = {
        "title": post.get("title") or "",
        "date": date_iso,
        "lastmod": lastmod_iso,
        "url": post.get("url", ""),
        "categories": post.get("categories", []) or [],
        "author": post.get("author", ""),
        "source": "wix",
        "source_guid": post.get("guid", ""),
    }

    body_html = post.get("content_html") or ""

    # If no body and requested, try scraping the page for article content
    if not body_html and scrape_if_missing and post.get("url"):
        try:
            page_html = http_get(post["url"]).decode("utf-8", errors="ignore")
            extracted = extract_article_from_html(page_html)
            if extracted:
                body_html = extracted
        except Exception:
            pass

    # Optionally download hero image from enclosure
    enclosure = post.get("enclosure") or post.get("image") or ""
    if download_hero and enclosure:
        ext = ext_from_url(enclosure)
        hero_path = folder / f"featured{ext}"
        if download_file(enclosure, hero_path):
            fm["image"] = f"./{hero_path.name}"

    # Convert body to requested format; only allow inline image rewrites when keeping HTML
    body_out = ""
    if body_format == "html":
        body_out = body_html
        if body_out and download_inline:
            body_out, _ = download_inline_images(body_out, folder / "images")
    elif body_format == "markdown":
        body_out = html_to_markdown(body_html) if body_html else ""
    else:  # plain
        body_out = html_to_plain(body_html) if body_html else ""

    # Emit Markdown with YAML front matter; keep HTML body in Markdown
    def yaml_dump(d: Dict[str, Any]) -> str:
        # Minimal YAML dumper to avoid dependencies; always quote strings safely
        def q(s: str) -> str:
            s = s.replace("\\", "\\\\").replace("\"", "\\\"")
            return f'"{s}"'

        lines: List[str] = []
        for k, v in d.items():
            if isinstance(v, list):
                lines.append(f"{k}:")
                for item in v:
                    if isinstance(item, str):
                        lines.append(f"  - {q(item)}")
                    else:
                        lines.append(f"  - {item}")
            elif isinstance(v, str):
                lines.append(f"{k}: {q(v)}")
            else:
                lines.append(f"{k}: {v}")
        return "\n".join(lines)

    md_parts: List[str] = ["---", yaml_dump(fm), "---", ""]
    if body_out:
        md_parts.append(body_out)
        md_parts.append("")
    else:
        md_parts.append("<!-- No content available in RSS. Consider re-running with --hydrate to scrape pages. -->")

    index_md.write_text("\n".join(md_parts), encoding="utf-8")
    return index_md


def export_posts(base_url: str, out_dir: str, structure: str, limit: Optional[int], download_hero: bool, scrape_if_missing: bool, download_inline: bool, body_format: str) -> Tuple[int, pathlib.Path]:
    base = guess_base_url(base_url)
    out_root = pathlib.Path(out_dir)
    ensure_dir(out_root)

    rss_map = parse_rss_full(base)
    sitemap_entries = parse_blog_sitemap(base)

    # Merge sitemap lastmod into rss_map where possible; add missing entries with minimal info
    # Build URL->lastmod map
    lastmods: Dict[str, str] = {e.get("url", ""): e.get("lastmod", "") for e in sitemap_entries if e.get("url")}
    for url, lastmod in lastmods.items():
        if url in rss_map:
            rss_map[url]["lastmod"] = lastmod
        else:
            rss_map[url] = {"url": url, "title": "", "published": "", "lastmod": lastmod, "categories": [], "author": "", "guid": "", "content_html": "", "enclosure": ""}

    # Order by date descending
    def sort_key(item: Tuple[str, Dict[str, Any]]):
        _, p = item
        d = parse_rfc822_date(p.get("published", "")) or parse_iso_date(p.get("lastmod", "")) or dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
        return d

    items = sorted(rss_map.items(), key=sort_key, reverse=True)
    if limit is not None:
        items = items[:limit]

    written = 0
    for _, post in items:
        write_markdown(
            post,
            out_root,
            structure=structure,
            download_hero=download_hero,
            scrape_if_missing=scrape_if_missing,
            download_inline=download_inline,
            body_format=body_format,
        )
        written += 1
    return written, out_root


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Export Wix blog posts to Markdown")
    ap.add_argument("--base-url", default=os.getenv("SOURCE_URL"), help="Base site URL (default: $SOURCE_URL)")
    ap.add_argument("--out-dir", default="content/posts", help="Output directory root")
    ap.add_argument("--structure", choices=["year/slug", "year/month/slug", "flat"], default="year/slug", help="Directory structure for posts")
    ap.add_argument("--limit", type=int, default=None, help="Limit number of posts to export")
    ap.add_argument("--no-hero", action="store_true", help="Do not download hero/enclosure image")
    ap.add_argument("--scrape", action="store_true", help="Scrape page HTML to fill missing content")
    ap.add_argument("--download-inline-images", action="store_true", help="Download inline images when keeping HTML body (ignored for markdown/plain)")
    ap.add_argument("--body-format", choices=["html", "markdown", "plain"], default="markdown", help="Body output format in Markdown file")
    args = ap.parse_args(argv)

    if not args.base_url:
        print("Error: --base-url or SOURCE_URL is required", file=sys.stderr)
        return 2

    count, out_root = export_posts(
        base_url=args.base_url,
        out_dir=args.out_dir,
        structure=args.structure,
        limit=args.limit,
    download_hero=not args.no_hero,
    scrape_if_missing=args.scrape,
    download_inline=args.download_inline_images,
    body_format=args.body_format,
    )
    print(f"Exported {count} posts to {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
