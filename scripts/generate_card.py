#!/usr/bin/env python3
"""
Regenerates profile-card.svg (ascii art + neofetch-style stats) using:
  - live data pulled from the GitHub REST + Search API
  - a local shallow-clone + `git log` pass over your own repos for commit/LOC counts
  - a cached fallback (stats-cache.json) so a rate-limited run never breaks the card

Run with:  python3 scripts/generate_card.py
Env vars:  GH_TOKEN (optional) - a PAT with public_repo, read:user scopes.
           Without it the script uses the GitHub API unauthenticated (lower rate limit,
           fine for a twice-daily cron).
"""
import json, os, re, subprocess, sys, tempfile, time, html, fnmatch
import urllib.request
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
CACHE_PATH = os.path.join(ROOT, "stats-cache.json")
ART_PATH = os.path.join(ROOT, "ascii-art.svg")
STATS_SVG_PATH = os.path.join(ROOT, "stats.svg")
CARD_PATH = os.path.join(ROOT, "profile-card.svg")

TOKEN = os.environ.get("GH_TOKEN", "").strip()

# ---------------------------------------------------------------- helpers --

def gh_request(url):
    """GET a GitHub API URL, retrying briefly on rate limit. Returns parsed JSON or None."""
    headers = {"User-Agent": "profile-card-generator"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    for attempt in range(5):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                time.sleep(5)
                continue
            print(f"HTTP error for {url}: {e}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"Error fetching {url}: {e}", file=sys.stderr)
            time.sleep(3)
    return None


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default


# ------------------------------------------------------------- uptime calc --

def compute_uptime(dob_str):
    dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
    today = datetime.now(timezone.utc).date()
    years = today.year - dob.year
    months = today.month - dob.month
    days = today.day - dob.day
    if days < 0:
        months -= 1
        prev_month = today.month - 1 or 12
        prev_year = today.year if today.month != 1 else today.year - 1
        import calendar
        days += calendar.monthrange(prev_year, prev_month)[1]
    if months < 0:
        years -= 1
        months += 12
    return f"{years} years, {months} months, {days} days"


# ------------------------------------------------------------- github data --

def fetch_profile(login):
    return gh_request(f"https://api.github.com/users/{login}")


def fetch_repos(login):
    repos, page = [], 1
    while True:
        batch = gh_request(f"https://api.github.com/users/{login}/repos?per_page=100&page={page}")
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def fetch_merged_pr_stats(login):
    """Merged PRs authored by `login` on repos NOT owned by `login` (i.e. real open-source contributions)."""
    data = gh_request(f"https://api.github.com/search/issues?q=author:{login}+type:pr+is:merged&per_page=50")
    if not data:
        return 0, 0, 0

    def owner_of(repo_url):
        # ".../repos/<owner>/<repo>" -> <owner>
        return repo_url.split("/repos/")[-1].split("/")[0]

    external = [it for it in data.get("items", []) if owner_of(it["repository_url"]).lower() != login.lower()]
    add, dele = 0, 0
    for it in external:
        pr = gh_request(it["pull_request"]["url"])
        if pr:
            add += pr.get("additions", 0) or 0
            dele += pr.get("deletions", 0) or 0
    return len(external), add, dele


# ------------------------------------------------------ own-repo LOC scan --

def matches_any(path, patterns):
    return any(fnmatch.fnmatch(path, p) or f"/{p}/" in f"/{path}/" for p in patterns)


def scan_own_repos(login, repos, exclude_dirs, exclude_files):
    total_commits, total_add, total_del = 0, 0, 0
    with tempfile.TemporaryDirectory() as tmp:
        for r in repos:
            if r.get("fork"):
                continue
            name = r["name"]
            url = r["clone_url"]
            dest = os.path.join(tmp, name)
            try:
                subprocess.run(
                    ["git", "clone", "--quiet", "--filter=blob:none", url, dest],
                    check=True, timeout=120,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                print(f"clone failed for {name}: {e}", file=sys.stderr)
                continue

            pathspec = ["."] + [f":(exclude)**/{d}/**" for d in exclude_dirs] + \
                       [f":(exclude)**/{f}" for f in exclude_files]

            try:
                commit_count = subprocess.run(
                    ["git", "-C", dest, "log", "--oneline"],
                    capture_output=True, text=True, timeout=60
                ).stdout.strip().splitlines()
                total_commits += len(commit_count)

                numstat = subprocess.run(
                    ["git", "-C", dest, "log", "--numstat", "--pretty=tformat:", "--"] + pathspec[1:],
                    capture_output=True, text=True, timeout=60
                ).stdout
                for line in numstat.splitlines():
                    parts = line.split("\t")
                    if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                        total_add += int(parts[0])
                        total_del += int(parts[1])
            except Exception as e:
                print(f"log scan failed for {name}: {e}", file=sys.stderr)
                continue
    return total_commits, total_add, total_del


# --------------------------------------------------------------- svg build --

FONT = "Space Mono, ui-monospace, SFMono-Regular, Menlo, Monaco, monospace"
FONT_SIZE = 40
LINE_H = 58
PAD_X = 44
PAD_Y = 44
BG = "#020303"
COL_TEXT, COL_LABEL, COL_DIM = "#e2e2e2", "#d19a66", "#4b5263"
COL_HEAD, COL_GREEN, COL_RED, COL_ORANGE = "#61afef", "#98c379", "#e06c75", "#e5a761"


def esc(s):
    return html.escape(s, quote=False)


def build_stats_svg(cfg, uptime_str, profile, star_total, own_commits, own_add, own_del,
                     ext_pr_count, ext_add, ext_del):
    lines = []

    def blank():
        lines.append([(".", COL_DIM)])

    def kv(label, value, dots=None):
        if dots is None:
            dots = max(2, 26 - len(label))
        lines.append([(". ", COL_DIM), (label, COL_LABEL), (":", COL_DIM),
                       (" " + "." * dots + " ", COL_DIM), (value, COL_TEXT)])

    def kv_colored(label, dots, parts):
        lines.append([(". ", COL_DIM), (label, COL_LABEL), (":", COL_DIM),
                       (" " + "." * dots + " ", COL_DIM)] + parts)

    def section(title):
        lines.append([("- ", COL_DIM), (title, COL_HEAD), (" ", COL_DIM),
                       ("-" * max(2, 36 - len(title)), COL_DIM)])

    lines.append([(cfg["display_name"], COL_HEAD), ("@", COL_DIM), ("github", COL_HEAD),
                  ("  ", COL_DIM), ("-" * 34, COL_DIM)])
    blank()
    kv("OS", cfg["os"])
    kv("Uptime", uptime_str, dots=8)
    blank()
    kv("Languages", cfg["languages_programming"], dots=6)
    kv("Focus", cfg["focus"], dots=13)
    blank()
    section("Contact")
    kv("GitHub", cfg["github_link"], dots=11)
    blank()
    section("GitHub Stats")
    kv_colored("Repos", 4, [(str(profile.get("public_repos", "?")), COL_TEXT),
                             ("   |   ", COL_DIM), ("Stars", COL_LABEL), (": ", COL_DIM),
                             (str(star_total), COL_ORANGE)])
    kv_colored("Followers", 0, [(str(profile.get("followers", "?")), COL_ORANGE),
                                 ("   |   ", COL_DIM), ("Following", COL_LABEL), (": ", COL_DIM),
                                 (str(profile.get("following", "?")), COL_TEXT)])
    kv_colored("Commits", 2, [(str(own_commits), COL_TEXT), ("   ", COL_DIM),
                               ("(own repos)", COL_DIM)])
    kv_colored("Open Source PRs", 0, [(str(ext_pr_count), COL_GREEN), (" merged", COL_DIM)])
    kv_colored("Lines of Code", 3, [
        ("( ", COL_DIM), (f"+{own_add + ext_add:,}++", COL_GREEN), (" , ", COL_DIM),
        (f"-{own_del + ext_del:,}--", COL_RED), (" )", COL_DIM)])

    max_chars = max(sum(len(t) for t, c in l) for l in lines)
    char_w = FONT_SIZE * 0.6
    width = int(PAD_X * 2 + max_chars * char_w) + 20
    height = int(PAD_Y * 2 + len(lines) * LINE_H)

    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="neofetch style stats">',
        f'  <rect width="100%" height="100%" fill="{BG}"/>',
        f'  <g font-family="{FONT}" font-size="{FONT_SIZE}">',
    ]
    y = PAD_Y
    for segs in lines:
        tspans = "".join(f'<tspan fill="{c}">{esc(t)}</tspan>' for t, c in segs)
        parts.append(f'    <text xml:space="preserve" x="{PAD_X}" y="{y}" '
                      f'dominant-baseline="text-before-edge" style="white-space:pre;">{tspans}</text>')
        y += LINE_H
    parts += ["  </g>", "</svg>"]
    return "\n".join(parts), width, height


def merge_cards(art_path, stats_svg_text, stats_w, stats_h):
    with open(art_path, encoding="utf-8") as f:
        art_content = f.read()
    m = re.search(r'<svg[^>]*viewBox="([\d.\s]+)"[^>]*>(.*)</svg>', art_content, re.DOTALL)
    art_vb_w, art_vb_h = (float(x) for x in m.group(1).split()[2:4])
    art_inner = m.group(2).strip()

    m2 = re.search(r'<svg[^>]*viewBox="([\d.\s]+)"[^>]*>(.*)</svg>', stats_svg_text, re.DOTALL)
    stats_vb_w, stats_vb_h = (float(x) for x in m2.group(1).split()[2:4])
    stats_inner = m2.group(2).strip()

    outer_h = 760
    gap = 48
    art_w = outer_h * (art_vb_w / art_vb_h)
    stats_w_disp = outer_h * (stats_vb_w / stats_vb_h)
    pad = 28
    total_w = art_w + gap + stats_w_disp + pad * 2
    total_h = outer_h + pad * 2

    return f'''<?xml version="1.0" encoding="utf-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{total_w:.0f}" height="{total_h:.0f}" viewBox="0 0 {total_w:.0f} {total_h:.0f}" role="img" aria-label="GitHub profile card">
  <rect width="100%" height="100%" rx="14" fill="{BG}"/>
  <rect x="1" y="1" width="{total_w-2:.0f}" height="{total_h-2:.0f}" rx="13" fill="none" stroke="#2a2f36" stroke-width="1.5"/>
  <svg x="{pad:.0f}" y="{pad:.0f}" width="{art_w:.0f}" height="{outer_h:.0f}" viewBox="0 0 {art_vb_w:.0f} {art_vb_h:.0f}" preserveAspectRatio="xMidYMid meet">
    {art_inner}
  </svg>
  <line x1="{pad+art_w+gap/2:.0f}" y1="{pad+20:.0f}" x2="{pad+art_w+gap/2:.0f}" y2="{pad+outer_h-20:.0f}" stroke="#2a2f36" stroke-width="1.5"/>
  <svg x="{pad+art_w+gap:.0f}" y="{pad:.0f}" width="{stats_w_disp:.0f}" height="{outer_h:.0f}" viewBox="0 0 {stats_vb_w:.0f} {stats_vb_h:.0f}" preserveAspectRatio="xMidYMid meet">
    {stats_inner}
  </svg>
</svg>
'''


# ------------------------------------------------------------------- main --

def main():
    cfg = load_json(CONFIG_PATH, {})
    cache = load_json(CACHE_PATH, {})
    login = cfg["github_login"]

    uptime_str = compute_uptime(cfg["dob"])

    profile = fetch_profile(login) or cache.get("profile", {})
    repos = fetch_repos(login) or cache.get("repos_raw", [])
    star_total = sum(r.get("stargazers_count", 0) for r in repos) if repos else cache.get("star_total", 0)

    ext_pr_count, ext_add, ext_del = fetch_merged_pr_stats(login)
    if ext_pr_count == 0 and "ext_pr_count" in cache:
        ext_pr_count, ext_add, ext_del = cache["ext_pr_count"], cache["ext_add"], cache["ext_del"]

    try:
        own_commits, own_add, own_del = scan_own_repos(
            login, repos, cfg.get("exclude_dirs", []), cfg.get("exclude_files", [])
        )
        if own_commits == 0:
            raise ValueError("scan returned zero, falling back to cache")
    except Exception as e:
        print(f"own-repo scan failed, using cache: {e}", file=sys.stderr)
        own_commits = cache.get("own_commits", 0)
        own_add = cache.get("own_add", 0)
        own_del = cache.get("own_del", 0)

    stats_svg_text, stats_w, stats_h = build_stats_svg(
        cfg, uptime_str, profile, star_total, own_commits, own_add, own_del,
        ext_pr_count, ext_add, ext_del,
    )
    with open(STATS_SVG_PATH, "w", encoding="utf-8") as f:
        f.write(stats_svg_text)

    card = merge_cards(ART_PATH, stats_svg_text, stats_w, stats_h)
    with open(CARD_PATH, "w", encoding="utf-8") as f:
        f.write(card)

    new_cache = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "repos_raw": repos,
        "star_total": star_total,
        "own_commits": own_commits,
        "own_add": own_add,
        "own_del": own_del,
        "ext_pr_count": ext_pr_count,
        "ext_add": ext_add,
        "ext_del": ext_del,
    }
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(new_cache, f, indent=2)

    print("profile-card.svg regenerated.")
    print(f"uptime={uptime_str} repos={profile.get('public_repos')} "
          f"followers={profile.get('followers')} own_commits={own_commits} "
          f"ext_pr_count={ext_pr_count}")


if __name__ == "__main__":
    main()
