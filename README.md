# Wix blog exporter to Markdown

This workspace fetches posts from a Wix site and exports them as Markdown files with YAML front matter, ready for static site generators.

## Usage

- Configure the source site in `.env`:
  - `SOURCE_URL=https://www.waccamaw.org/`

- List posts (preview):

```bash
just posts
```

- Export posts to Markdown (year/slug structure):

```bash
just export
```

- Alternate structures:

```bash
just export-monthly   # content/posts/YYYY/MM/slug/index.md
just export-flat      # content/posts/slug/index.md
```

- JSON preview:

```bash
just posts-json
```

- Re-run clean:

```bash
just clean
just export
```

## Output structure

Default layout:

```
content/
  posts/
    2025/
      autumn-equinox-2025/
        index.md         # Markdown with YAML front matter and HTML body
        featured.jpg     # Hero image when available
    2024/
      december-2024-open-meeting-summary/
        index.md
```

## Notes

- Body content is embedded as HTML from the RSS `content:encoded` field (Wix provides full content there).
- Front matter includes: `title`, `date`, `lastmod`, `url`, `categories`, `author`, `source`, `source_guid`, and optional `image` (local featured image).
- To skip downloading hero images, pass `--no-hero`:

```bash
python3 scripts/export_wix_blog_to_md.py --no-hero
```

- If a few posts lack content in RSS, consider scraping as a follow-up enhancement.
