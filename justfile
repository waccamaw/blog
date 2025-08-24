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