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


def http_get_text(url: str, timeout: float = 30.0) -> Optional[str]:
    try:
        return http_get(url, timeout=timeout).decode("utf-8", errors="ignore")
    except Exception:
        return None


def get_doc_url_hints() -> Dict[str, str]:
    """Parse DOC_URL_HINTS env var into a map of lowercase filename -> URL.

    Format examples:
      DOC_URL_HINTS="PW2023Public.xlsx=https://download-files...., report.pdf=https://.../report.pdf"
      DOC_URL_HINTS="one.docx=https://...;two.pdf=https://..."
    """
    hints: Dict[str, str] = {}
    raw = os.getenv("DOC_URL_HINTS", "").strip()
    if not raw:
        return hints
    # split by comma or semicolon
    parts = re.split(r"[;,]\s*", raw)
    for part in parts:
        if not part:
            continue
        if "=" not in part:
            continue
        name, url = part.split("=", 1)
        name = os.path.basename(name.strip()).lower()
        url = url.strip()
        if name and url:
            hints[name] = url
    return hints


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


def _parse_front_matter(path: pathlib.Path) -> Dict[str, Any]:
    """Parse minimal YAML front matter from an index.md written by this tool."""
    try:
        txt = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not txt.startswith("---\n"):
        return {}
    parts = txt.split("\n---\n", 1)
    if len(parts) < 2:
        return {}
    fm_text = parts[0][4:]  # strip leading '---\n'
    data: Dict[str, Any] = {}
    current_key: Optional[str] = None
    list_mode = False
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key:
            # list item
            item = line[4:]
            if item.startswith('"') and item.endswith('"'):
                item = item[1:-1].replace('\\"', '"').replace('\\\\', '\\')
            data.setdefault(current_key, []).append(item)
            list_mode = True
            continue
        # new key
        m = re.match(r"([A-Za-z0-9_]+):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        current_key = key
        list_mode = False
        if val == "":
            data[key] = []
        else:
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1].replace('\\"', '"').replace('\\\\', '\\')
            data[key] = val
    return data


def _existing_last_modified(index_md: pathlib.Path) -> Optional[dt.datetime]:
    fm = _parse_front_matter(index_md)
    s = fm.get("lastmod") or fm.get("date")
    if isinstance(s, str):
        return parse_iso_date(s) or parse_rfc822_date(s)  # tolerate either
    return None


def ext_from_url(url: str, default: str = ".jpg") -> str:
    path = urllib.parse.urlparse(url).path
    _, ext = os.path.splitext(path)
    return ext or default


def largest_image_candidates(url: str) -> List[str]:
    """Return best-effort list of image URL candidates preferring the largest/original.

    For Wix static URLs (static.wixstatic.com), we strip any transformation segment like
    /v1/fill/... or /v1/fit/... to use the original asset path. Query params are dropped.
    """
    try:
        pu = urllib.parse.urlparse(url)
        path = pu.path
        # Drop any transformer path that follows the original asset (starts with /v1/)
        if "/v1/" in path:
            path = path.split("/v1/", 1)[0]
        # Rebuild without query/fragment
        normalized = urllib.parse.urlunparse((pu.scheme, pu.netloc, path, "", "", ""))
        candidates = []
        if normalized and normalized != url:
            candidates.append(normalized)
        candidates.append(url)
        # De-dup while preserving order
        seen = set()
        uniq: List[str] = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq
    except Exception:
        return [url]


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
        # Deterministic destination path
        dest = folder / base
        i = 1
        while dest.exists() and i < 2:  # keep original name if exists
            name, ext = os.path.splitext(base)
            dest = folder / f"{name}-{i}{ext}"
            i += 1
        # Try largest/original first, then fallback
        ok = False
        # If the preferred dest already exists, reuse it without downloading
        if (folder / base).exists():
            ok = True
        for cand in largest_image_candidates(url):
            if download_file(cand, dest):
                ok = True
                break
        if ok:
            count += 1
            return full.replace(url, f"./{folder.name}/{dest.name}")
        return full
    new_html = re.sub(r"<img[^>]+src=\"([^\"]+)\"[^>]*>", repl, html_body, flags=re.I)
    return new_html, count


def rewrite_markdown_images(md: str, folder: pathlib.Path) -> Tuple[str, int]:
    """Download all remote images referenced by Markdown (both image syntax and plain links) and rewrite to local paths.

    - Handles: ![alt](url) and [alt](url) when url looks like an image
    - Deduplicates repeated URLs within the same document

    Returns: (new_markdown, downloaded_count)
    """
    ensure_dir(folder)
    count = 0
    url_map: Dict[str, str] = {}

    def is_image_url(u: str) -> bool:
        if not u:
            return False
        pu = urllib.parse.urlparse(u)
        path = pu.path.lower()
        _, ext = os.path.splitext(path)
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}:
            return True
        # Heuristic: wix static media paths
        if "static.wixstatic.com" in (pu.netloc or "") and "/media/" in path:
            return True
        return False

    def canonical_image_key(u: str) -> str:
        try:
            first = largest_image_candidates(u)[0]
            pu = urllib.parse.urlparse(first)
            return urllib.parse.urlunparse((pu.scheme, pu.netloc, pu.path, "", "", ""))
        except Exception:
            return u

    def download_and_local(u: str) -> Optional[str]:
        nonlocal count
        key = canonical_image_key(u)
        if key in url_map:
            return url_map[key]
        if not (u.startswith("http://") or u.startswith("https://")):
            return None
        if not is_image_url(u):
            return None
        # Use canonical key to derive filename, so variants map consistently
        base = os.path.basename(urllib.parse.urlparse(u).path) or f"image-{count}.jpg"
        dest = folder / base
        i = 1
        while dest.exists() and i < 2:
            name, ext = os.path.splitext(base)
            dest = folder / f"{name}-{i}{ext}"
            i += 1
        ok = False
        if (folder / base).exists():
            ok = True
        for cand in largest_image_candidates(u):
            if download_file(cand, dest):
                ok = True
                break
        if not ok:
            return None
        count += 1
        local = f"./{folder.name}/{dest.name}"
        url_map[key] = local
        return local

    def repl_img(m: re.Match) -> str:
        alt = m.group(1) or ""
        url = m.group(2) or ""
        title = (m.group(3) or "").strip()
        local = download_and_local(url)
        if not local:
            return m.group(0)
        return f"![{alt}]({local} \"{title}\")" if title else f"![{alt}]({local})"

    def repl_link(m: re.Match) -> str:
        alt = m.group(1) or ""
        url = m.group(2) or ""
        title = (m.group(3) or "").strip()
        local = download_and_local(url)
        if not local:
            return m.group(0)
        # Convert link-to-image into markdown image
        return f"![{alt}]({local} \"{title}\")" if title else f"![{alt}]({local})"

    # Pass 1: Markdown image syntax with raw quotes
    pattern_img1 = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)")
    md = pattern_img1.sub(repl_img, md)
    # Pass 2: Markdown image syntax with &quot; title
    pattern_img2 = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)")
    md = pattern_img2.sub(repl_img, md)
    # Pass 3: Plain links that look like images (raw quotes)
    pattern_link1 = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)")
    md = pattern_link1.sub(repl_link, md)
    # Pass 4: Plain links with &quot; titles
    pattern_link2 = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)")
    md = pattern_link2.sub(repl_link, md)
    return md, count


def rewrite_markdown_documents(md: str, folder: pathlib.Path) -> Tuple[str, int]:
    """Download remote document links (PDF and Microsoft Office formats) and rewrite to local paths.

    Handles Markdown links [text](url) where url ends with one of: .pdf, .doc, .docx, .xls, .xlsx, .ppt, .pptx
    Returns: (new_markdown, downloaded_count)
    """
    ensure_dir(folder)
    count = 0
    url_map: Dict[str, str] = {}

    exts = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}

    def is_doc_url(u: str) -> bool:
        if not u or not (u.startswith("http://") or u.startswith("https://")):
            return False
        pu = urllib.parse.urlparse(u)
        _, ext = os.path.splitext(pu.path.lower())
        return ext in exts

    def download_and_local(u: str) -> Optional[str]:
        nonlocal count
        if u in url_map:
            return url_map[u]
        if not is_doc_url(u):
            return None
        pu = urllib.parse.urlparse(u)
        base = os.path.basename(pu.path) or f"file-{count}.bin"
        dest = folder / base
        i = 1
        while dest.exists() and i < 2:
            name, ext = os.path.splitext(base)
            dest = folder / f"{name}-{i}{ext}"
            i += 1
        if (folder / base).exists():
            ok = True
        else:
            ok = download_file(u, dest)
        if not ok:
            return None
        count += 1
        local = f"./{folder.name}/{dest.name}"
        url_map[u] = local
        return local

    def repl_link(m: re.Match) -> str:
        text = m.group(1) or ""
        url = m.group(2) or ""
        title = (m.group(3) or "").strip()
        local = download_and_local(url)
        if not local:
            return m.group(0)
        return f"[{text}]({local} \"{title}\")" if title else f"[{text}]({local})"

    pattern1 = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)")
    md = pattern1.sub(repl_link, md)
    pattern2 = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)")
    md = pattern2.sub(repl_link, md)
    return md, count


def _extract_doc_links_from_html(page_html: str, base_url: Optional[str]) -> Dict[str, str]:
    """Return map of lowercase basename -> absolute URL for doc links found in HTML.

    Matches:
    - href="...ext" and href='...ext'
    - Any http(s) URL in the HTML text that ends with a supported extension
    """
    out: Dict[str, str] = {}
    # href with double or single quotes
    doc_href = re.compile(r"href=([\"'])([^\"']+\.(?:pdf|docx?|xlsx?|pptx?))(?:\?[^\"']*)?\1", re.I)
    for m in doc_href.finditer(page_html):
        raw = m.group(2)
        url = urllib.parse.urljoin(base_url or "", raw)
        pu = urllib.parse.urlparse(url)
        base = os.path.basename(pu.path)
        key = base.lower()
        if base and key not in out:
            out[key] = url
        # Map by dn= original filename if present
        q = urllib.parse.parse_qs(pu.query)
        dn = q.get("dn", [None])[0]
        if dn:
            dn_base = os.path.basename(dn)
            dn_key = dn_base.lower()
            if dn_base and dn_key not in out:
                out[dn_key] = url
    # Generic URLs in the text
    url_text = re.compile(r"https?://[^\s\"'<>]+\.(?:pdf|docx?|xlsx?|pptx?)(?:\?[^\s\"'<>]*)?", re.I)
    for m in url_text.finditer(page_html):
        url = m.group(0)
        pu = urllib.parse.urlparse(url)
        base = os.path.basename(pu.path)
        key = base.lower()
        if base and key not in out:
            out[key] = url
        q = urllib.parse.parse_qs(pu.query)
        dn = q.get("dn", [None])[0]
        if dn:
            dn_base = os.path.basename(dn)
            dn_key = dn_base.lower()
            if dn_base and dn_key not in out:
                out[dn_key] = url
    return out


def rewrite_bare_doc_filenames(md: str, page_url: Optional[str], folder: pathlib.Path) -> Tuple[str, int]:
    """Find bare document filenames in Markdown text, locate their URLs via page HTML, download, and rewrite to [name](./files/name)."""
    ensure_dir(folder)
    # Find candidate filenames not obviously part of a Markdown link already
    filename_re = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9_.-]{1,200}\.(?:pdf|docx?|xlsx?|pptx?))\b")

    # Remove fenced code blocks to avoid accidental replacements inside code
    code_fence_re = re.compile(r"```[\s\S]*?```", re.M)
    placeholders: List[str] = []
    def _stash(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"@@CODEFENCE{len(placeholders)-1}@@"
    work = code_fence_re.sub(_stash, md)

    # Build link map from page + optional env hints
    doc_map: Dict[str, str] = {}
    if page_url:
        page_html = http_get_text(page_url)
        if page_html:
            doc_map = _extract_doc_links_from_html(page_html, page_url)
    # Merge hints (do not overwrite discovered URLs)
    hints = get_doc_url_hints()
    for k, v in hints.items():
        doc_map.setdefault(k, v)

    downloaded: Dict[str, str] = {}
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        name = m.group(1)
        # Skip if already part of a markdown link: [text](...name...) or inline link immediately around
        start = m.start()
        end = m.end()
        left = work[max(0, start-2):start]
        right = work[end:end+2]
        if left.endswith("(") or right.startswith(")"):
            return name
        key = name.lower()
        url = doc_map.get(key)
        if not url:
            # If there's exactly one doc link found on page, use it as a fallback
            if len(doc_map) == 1:
                url = next(iter(doc_map.values()))
            else:
                # try extension-based fallback when only one candidate with same ext exists
                _, ext = os.path.splitext(key)
                candidates = [v for k, v in doc_map.items() if k.endswith(ext)]
                if len(candidates) == 1:
                    url = candidates[0]
                else:
                    return name
        # Download
        dest = folder / name
        i = 1
        while dest.exists() and i < 2:
            stem, ext = os.path.splitext(name)
            dest = folder / f"{stem}-{i}{ext}"
            i += 1
        if (folder / name).exists():
            pass
        elif not download_file(url, dest):
            return name
        local = f"./{folder.name}/{dest.name}"
        downloaded[name] = local
        count += 1
        return f"[{name}]({local})"

    work = filename_re.sub(repl, work)

    # Restore code blocks
    def _unstash(m: re.Match) -> str:
        idx = int(m.group(1))
        return placeholders[idx]
    work = re.sub(r"@@CODEFENCE(\d+)@@", _unstash, work)
    return work, count


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
        self.in_blockquote = False
        self.in_script = False
        self.in_style = False

    def _ensure_block_sep(self, lines: int = 2) -> None:
        # Ensure a blank line separation between blocks
        text = "".join(self.out)
        if not text:
            return
        # Normalize end to at most one blank line, then add required
        text = re.sub(r"\n+$", "\n", text)
        self.out[:] = [text]
        self.out.append("\n" * lines)

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]):
        attrs = dict(attrs_list)
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            if tag == "script":
                self.in_script = True
            elif tag == "style":
                self.in_style = True
            return
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
            self.in_blockquote = True
        elif tag == "img":
            alt = attrs.get("alt", "") or ""
            src = attrs.get("src", "") or ""
            if src:
                # Represent as a link to keep content light
                text = alt or "image"
                # Use Markdown image syntax so we can later download and rewrite
                self.out.append(f"![{text}]({src})")

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            if tag == "script":
                self.in_script = False
            elif tag == "style":
                self.in_style = False
            return
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
        elif tag in {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.out.append("\n\n")
        elif tag == "blockquote":
            # End blockquote
            self.in_blockquote = False
            self.out.append("\n\n")

    def handle_data(self, data: str):
        if not data:
            return
        if self.in_script or self.in_style:
            return
        if self.in_blockquote:
            # Prefix each non-empty line with "> "
            lines = data.splitlines()
            for i, ln in enumerate(lines):
                pref = "> " if ln.strip() else ""
                self.out.append(pref + ln)
                if i < len(lines) - 1:
                    self.out.append("\n")
        else:
            self.out.append(data)

    def getvalue(self) -> str:
        text = "".join(self.out)
        # Normalize excessive blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Trim whitespace around lines
        lines = [ln.rstrip() for ln in text.splitlines()]
        return "\n".join(lines).strip() + "\n"


def _strip_markdown_css_js_noise(md: str) -> str:
    """Remove residual CSS/JS-like blocks that sometimes leak from Wix embeds after conversion.

    Heuristics:
    - Drop blocks that look like CSS rules (lines with selectors + { ... }) until braces balance.
    - Drop common JS noise lines (window., document., requestAnimationFrame, try { } catch, console.).
    """
    lines = md.splitlines()
    out: List[str] = []
    in_css = False
    brace_depth = 0
    css_start_re = re.compile(r"\s*(?:@media|@keyframes|[.#][\w-].*\{|[a-zA-Z][\w\-\s,#.:>\[\]=]+\{)")
    js_noise_re = re.compile(r"\b(window\.|document\.|requestAnimationFrame|getBoundingClientRect|console\.)")
    for ln in lines:
        if in_css:
            brace_depth += ln.count("{") - ln.count("}")
            if brace_depth <= 0:
                in_css = False
            continue
        # Detect CSS block start on this line
        if css_start_re.match(ln):
            in_css = True
            brace_depth = ln.count("{") - ln.count("}")
            # If CSS opens and closes on same line, exit immediately
            if brace_depth <= 0:
                in_css = False
            continue
        # Skip common JS noise lines
        if js_noise_re.search(ln) or re.match(r"\s*try\s*\{\s*$", ln) or re.match(r"\s*}\s*catch\b", ln):
            continue
        out.append(ln)
    txt = "\n".join(out)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip() + "\n"


def html_to_markdown(html_text: str) -> str:
    # Remove scripts, styles, and noscript blocks before conversion
    cleaned = re.sub(r"<script[\s\S]*?</script>", " ", html_text, flags=re.I)
    cleaned = re.sub(r"<style[\s\S]*?</style>", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"<noscript[\s\S]*?</noscript>", " ", cleaned, flags=re.I)
    parser = MarkdownHTMLParser()
    parser.feed(cleaned)
    md = parser.getvalue()
    # Post-process to strip any lingering CSS/JS-like noise
    return _strip_markdown_css_js_noise(md)


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
    # Deduplicate/sort categories
    cats = list(dict.fromkeys(post.get("categories", []) or []))
    cats.sort()

    fm: Dict[str, Any] = {
        "title": post.get("title") or "",
        "date": date_iso,
        "lastmod": lastmod_iso,
        "url": post.get("url", ""),
        "categories": cats,
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
        ok = False
        if hero_path.exists():
            ok = True
        for cand in largest_image_candidates(enclosure):
            if download_file(cand, hero_path):
                ok = True
                break
        if ok:
            fm["image"] = f"./{hero_path.name}"

    # Convert body to requested format; only allow inline image rewrites when keeping HTML
    body_out = ""
    if body_format == "html":
        body_out = body_html
        if body_out and download_inline:
            body_out, _ = download_inline_images(body_out, folder / "images")
    elif body_format == "markdown":
        body_out = html_to_markdown(body_html) if body_html else ""
        if body_out and download_inline:
            body_out, _ = rewrite_markdown_images(body_out, folder / "images")
            # Also download and rewrite document links
            body_out, _ = rewrite_markdown_documents(body_out, folder / "files")
            # Also detect bare filenames like PW2023Public.xlsx and link them
            body_out, _ = rewrite_bare_doc_filenames(body_out, post.get("url"), folder / "files")
    else:  # plain
        body_out = html_to_plain(body_html) if body_html else ""

    # Second-chance scrape: if conversion produced no content, try fetching page and extracting broader content
    if not body_out and scrape_if_missing and post.get("url"):
        try:
            page_html = http_get(post["url"]).decode("utf-8", errors="ignore")
            extracted = extract_article_from_html(page_html)
            if not extracted:
                # Fallback to <main> or whole <body> if article couldn't be found
                m = re.search(r"<main[\s\S]*?</main>", page_html, re.I)
                if m:
                    extracted = m.group(0)
                else:
                    m = re.search(r"<body[^>]*>([\s\S]*?)</body>", page_html, re.I)
                    extracted = m.group(1) if m else ""
            if extracted:
                if body_format == "html":
                    body_out = extracted
                    if download_inline:
                        body_out, _ = download_inline_images(body_out, folder / "images")
                elif body_format == "markdown":
                    body_out = html_to_markdown(extracted)
                    if download_inline and body_out:
                        body_out, _ = rewrite_markdown_images(body_out, folder / "images")
                        body_out, _ = rewrite_markdown_documents(body_out, folder / "files")
                        body_out, _ = rewrite_bare_doc_filenames(body_out, post.get("url"), folder / "files")
                else:
                    body_out = html_to_plain(extracted)
        except Exception:
            pass

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
        md_parts.append("<!-- No content available in RSS. Consider re-running with --scrape to fetch page content. -->")

    index_md.write_text("\n".join(md_parts), encoding="utf-8")
    return index_md


def export_posts(base_url: str, out_dir: str, structure: str, limit: Optional[int], download_hero: bool, scrape_if_missing: bool, download_inline: bool, body_format: str, only_changed: bool = False) -> Tuple[int, pathlib.Path]:
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
        # Skip unchanged posts if requested
        if only_changed:
            # Compute target folder/index path
            # Reuse logic from write_markdown to derive folder
            pub_dt = parse_rfc822_date(post.get("published", ""))
            lastmod_dt = parse_iso_date(post.get("lastmod", ""))
            date_dt = pub_dt or lastmod_dt or dt.datetime.now(dt.timezone.utc)
            year = f"{date_dt.year:04d}"
            slug = slug_from_url(post.get("url", "")) if post.get("url") else path_safe_slug(post.get("title", "post"))
            if structure == "year/slug":
                folder = out_root / year / slug
            elif structure == "year/month/slug":
                folder = out_root / year / f"{date_dt.month:02d}" / slug
            elif structure == "flat":
                folder = out_root / slug
            else:
                folder = out_root / year / slug
            index_md = folder / "index.md"
            if index_md.exists():
                local_dt = _existing_last_modified(index_md)
                remote_dt = parse_iso_date(post.get("lastmod", "")) or parse_rfc822_date(post.get("published", ""))
                if local_dt and remote_dt and remote_dt <= local_dt:
                    continue
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
    ap.add_argument("--download-inline-images", action="store_true", help="Download inline images and rewrite references (supports html and markdown body formats)")
    ap.add_argument("--body-format", choices=["html", "markdown", "plain"], default="markdown", help="Body output format in Markdown file")
    ap.add_argument("--only-changed", action="store_true", help="Only export posts whose lastmod/published is newer than local index.md")
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
    only_changed=args.only_changed,
    )
    print(f"Exported {count} posts to {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
