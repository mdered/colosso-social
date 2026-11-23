#!/usr/bin/env python3
"""Publishes the Instagram posts in queue.json when they're due. Runs every 15 minutes on GitHub Actions.

Test mode (default): checks every due post (images reachable, caption length) and prints what it would publish.
Live mode: only when the repository variable LIVE is "true".

    LIVE=true IG_IT_TOKEN=… IG_IT_USER_ID=… python publish.py      # what the workflow runs

Rules that keep it safe:
- a post is published once: its id goes into published.json, which the workflow commits back;
- at most one post per account per run;
- a post more than 12 hours late is never published, only marked "missed" (so a paused job never floods the feed);
- an account without its secrets is skipped.
Images are served by GitHub Pages from this repository (Instagram downloads them from a public URL).
Standard library only.
"""
import datetime, json, os, sys, time, urllib.error, urllib.parse, urllib.request

API = "https://graph.facebook.com/v21.0"
HERE = os.path.dirname(os.path.abspath(__file__))
BASE_URL = os.environ.get("MEDIA_BASE_URL", "https://mdered.github.io/colosso-social/")
LIVE = os.environ.get("LIVE", "").strip().lower() == "true"
LIVE_ACCOUNTS = {a.strip() for a in os.environ.get("LIVE_ACCOUNTS", "").split(",") if a.strip()}  # empty = all accounts

def is_live(account):
    return LIVE and (not LIVE_ACCOUNTS or account in LIVE_ACCOUNTS)
MAX_LATE = datetime.timedelta(hours=12)
CAPTION_LIMIT = 2200


def load(name, default):
    path = os.path.join(HERE, name)
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else default


def call(method, path, token, **params):
    params["access_token"] = token
    data = urllib.parse.urlencode(params).encode()
    url = f"{API}/{path}"
    req = urllib.request.Request(url, data=data, method="POST") if method == "POST" else \
          urllib.request.Request(f"{url}?{data.decode()}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        err = json.loads(e.read() or b"{}").get("error", {})
        raise RuntimeError(f"{method} {path}: {err.get('message', e.code)}") from None


def wait_ready(container, token):
    time.sleep(10)
    for _ in range(15):                                       # up to 5 minutes, one check every 20 s
        status = call("GET", container, token, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container {container}: {status}")
        time.sleep(20)                                            # few, spaced checks: Meta limits API calls
    raise RuntimeError(f"container {container}: not ready after 5 minutes")


def check_image(url):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        kind = r.headers.get("Content-Type", "")
        if r.status != 200 or "jpeg" not in kind:
            raise RuntimeError(f"{url}: HTTP {r.status}, {kind}")


def publish(item, ig_user, token):
    urls = [BASE_URL + p for p in item["images"]]
    if len(urls) == 1:
        parent = call("POST", f"{ig_user}/media", token, image_url=urls[0], caption=item["caption"])["id"]
    else:
        children = []
        for url in urls:
            child = call("POST", f"{ig_user}/media", token, image_url=url, is_carousel_item="true")["id"]
            wait_ready(child, token)
            children.append(child)
        parent = call("POST", f"{ig_user}/media", token, media_type="CAROUSEL",
                      children=",".join(children), caption=item["caption"])["id"]
    wait_ready(parent, token)
    return call("POST", f"{ig_user}/media_publish", token, creation_id=parent)["id"]


def published_on_github(item_id):
    """True if the latest published.json on GitHub already has this post. A run that GitHub delayed may hold an old
    copy, in which a post published since then still looks due (this caused a double post on 2026-11-16)."""
    import subprocess
    try:
        subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=HERE, check=True, timeout=60)
        latest = json.loads(subprocess.run(["git", "show", "origin/main:published.json"], cwd=HERE, check=True,
                                           capture_output=True, timeout=60).stdout or b"{}")
    except Exception as e:
        print(f"can't read the latest published.json ({e}): not publishing in this run")
        return True
    return item_id in latest


def main():
    queue, done = load("queue.json", []), load("published.json", {})
    now = datetime.datetime.now(datetime.timezone.utc)
    used, changed, failed = set(), False, False
    mode = ("LIVE" + (f" for {', '.join(sorted(LIVE_ACCOUNTS))}, test mode for the others" if LIVE_ACCOUNTS else "")) if LIVE \
        else "TEST MODE (nothing is published)"
    print(f"{mode} · {now:%Y-%m-%d %H:%M} UTC · {len(queue)} queued, {len(done)} done")
    for item in sorted(queue, key=lambda i: i["when"]):
        when = datetime.datetime.fromisoformat(item["when"])
        acc = item["account"]
        if item["id"] in done or when > now or acc in used:
            continue
        token, ig_user = os.environ.get(f"IG_{acc.upper()}_TOKEN"), os.environ.get(f"IG_{acc.upper()}_USER_ID")
        if not token or not ig_user:
            print(f"skip {item['id']}: no secrets for account '{acc}'")
            continue
        if now - when > MAX_LATE:
            print(f"MISSED {item['id']} (due {item['when']}): too late, not published")
            if is_live(acc):
                done[item["id"]] = {"status": "missed", "at": now.isoformat(timespec="seconds")}
                changed = True
            continue
        used.add(acc)
        try:
            if len(item["caption"]) > CAPTION_LIMIT:
                raise RuntimeError(f"caption is {len(item['caption'])} characters (limit {CAPTION_LIMIT})")
            for p in item["images"]:
                check_image(BASE_URL + p)
            if not is_live(acc):
                print(f"would publish {item['id']} to {acc}: {len(item['images'])} image(s), caption {len(item['caption'])} chars")
                continue
            if published_on_github(item["id"]):
                print(f"skip {item['id']}: already published (latest state on GitHub)")
                continue
            media = publish(item, ig_user, token)
            done[item["id"]] = {"status": "published", "media_id": media, "at": now.isoformat(timespec="seconds")}
            changed = True
            print(f"PUBLISHED {item['id']} to {acc} (media {media})")
        except Exception as e:
            failed = True
            print(f"FAILED {item['id']}: {e}")
    if changed:
        with open(os.path.join(HERE, "published.json"), "w", encoding="utf-8") as f:
            json.dump(done, f, indent=2, sort_keys=True)
    upcoming = [i for i in sorted(queue, key=lambda i: i["when"])
                if i["id"] not in done and datetime.datetime.fromisoformat(i["when"]) > now]
    if upcoming:
        print(f"next: {upcoming[0]['id']} at {upcoming[0]['when']}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
