"""Copy this file to config.py and fill in your own learning-platform URLs."""

# Paste the full directory URL, including its # fragment and query parameters.
CATALOG_URL = ""

# Optional: a single course URL used by download_official.py and probe.py.
COURSE_URL = ""

# Optional: HTTPS origin only. Normally inferred from CATALOG_URL / COURSE_URL.
SITE_ORIGIN = ""

# Local Chrome debugging endpoint; this is not a learning-platform URL.
CDP_URL = "http://127.0.0.1:9222"

# Public vendor API domains for the Yunxuetang adapter, without scheme or path.
# Add the exact API host for a private deployment if needed.
API_HOST_SUFFIXES = ("yunxuetang.cn", "yxt.com")

# Blank means the current month, e.g. course-title-YY-MM. Set a fixed suffix
# when resuming downloads that should stay in the same directory.
FOLDER_SUFFIX = ""
