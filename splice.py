"""Splice the awgit body + identity into the canonical Aitherium page template."""
import re

SRC = r"C:\Users\wzns\AppData\Local\Temp\awgit-pages\docs\index.html"
BODY = r"C:\Users\wzns\AppData\Local\Temp\awgit-pages\awgit-body.html"

html = open(SRC, encoding="utf-8").read()
body = open(BODY, encoding="utf-8").read().strip()

# 1. title + meta description + og tags
html = re.sub(
    r"<title>[^<]*</title>",
    "<title>awgit — the git that scales to agents</title>", html, count=1,
)
html = re.sub(
    r'<meta name="description" content="[^"]*">',
    '<meta name="description" content="Semantic version control on top of git: every commit becomes an edit-op on stable node ids, with verified-identity attribution and differential sync.">',
    html, count=1,
)
html = re.sub(
    r'<meta property="og:title" content="[^"]*">',
    '<meta property="og:title" content="awgit — the git that scales to agents">',
    html, count=1,
)
html = re.sub(
    r'<meta property="og:description" content="[^"]*">',
    '<meta property="og:description" content="Semantic version control on top of git: every commit becomes an edit-op on stable node ids, with verified-identity attribution and differential sync.">',
    html, count=1,
)

# 2. per-repo accent override (warm -> cyan)
override = re.search(
    r"<style>:root\{--accent:oklch\([^)]*\);--accent-a04[^<]*</style>", html,
)
assert override, "accent override block not found"
cyan = ("<style>:root{--accent:oklch(0.80 0.13 195);--accent-a04:oklch(0.80 0.13 195 / 4%);"
        "--accent-a10:oklch(0.80 0.13 195 / 10%);--accent-a30:oklch(0.80 0.13 195 / 30%);"
        "--accent-a50:oklch(0.80 0.13 195 / 50%);--accent-glow:oklch(0.80 0.13 195 / 18%);}</style>")
html = html.replace(override.group(0), cyan, 1)

# 3. body: replace from <body> through </footer> (inclusive) with the awgit body
body_start = html.find("<body>")
footer_end = html.find("</footer>", body_start)
assert body_start != -1 and footer_end != -1, "body/footer markers not found"
footer_end += len("</footer>")
html = html[:body_start] + body + "\n" + html[footer_end:]

open(SRC, "w", encoding="utf-8").write(html)
print("spliced OK; new size:", len(html))
