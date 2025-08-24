set dotenv-load
# run the exporter tool
run:
    echo $SOURCE_URL

# List Wix blog posts (table)
posts:
    python3 scripts/fetch_wix_blog.py --format table

# JSON output
posts-json:
    python3 scripts/fetch_wix_blog.py --format json

# Hydrate missing titles by fetching pages
posts-hydrate:
    python3 scripts/fetch_wix_blog.py --format table --hydrate

# Export posts to Markdown under content/posts/YYYY/slug/index.md
export:
    python3 scripts/export_wix_blog_to_md.py --out-dir content/posts --structure year/slug

# Alternate structure with month subfolders
export-monthly:
    python3 scripts/export_wix_blog_to_md.py --out-dir content/posts --structure year/month/slug

# Flat structure
export-flat:
    python3 scripts/export_wix_blog_to_md.py --out-dir content/posts --structure flat

# Clean generated content
clean:
    rm -rf content/posts

# Export with scraping to fill missing content and download inline images
export-rich:
    python3 scripts/export_wix_blog_to_md.py --out-dir content/posts --structure year/slug --scrape --download-inline-images

# Export content with Markdown body (no raw HTML)
export-md:
    python3 scripts/export_wix_blog_to_md.py --out-dir content/posts --structure year/slug --scrape --body-format markdown

# Export content with plain-text body
export-txt:
    python3 scripts/export_wix_blog_to_md.py --out-dir content/posts --structure year/slug --scrape --body-format plain

# Export Markdown with a limit parameter
export-md-limit limit:
    python3 scripts/export_wix_blog_to_md.py --out-dir content/posts --structure year/slug --scrape --body-format markdown --limit {{limit}}

# Clean then export Markdown with limit
clean-export-md-limit limit:
    just clean
    just export-md-limit {{limit}}