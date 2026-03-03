import json
import os
import sys
import time

# Support vendored dependencies installed via pip --target lib/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))

import stashapi.log as log  # noqa: E402
from stashapi.stashapp import StashInterface  # noqa: E402

PLUGIN_ID = "stash-scrape"

# ---------------------------------------------------------------------------
# GraphQL queries & mutations
# ---------------------------------------------------------------------------

PLUGIN_AND_STASHBOX_CONFIG = """
query Configuration {
    configuration {
        general {
            stashBoxes { endpoint api_key name }
        }
        plugins
    }
}
"""

FIND_SCENES_PAGE = """
query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType) {
    findScenes(filter: $filter, scene_filter: $scene_filter) {
        count
        scenes {
            id
            title
            details
            date
            urls
            director
            organized
            stash_ids { stash_id endpoint }
            studio     { id name }
            performers { id name }
            tags       { id name }
        }
    }
}
"""

FIND_SCENE_BY_ID = """
query FindScene($id: ID!) {
    findScene(id: $id) {
        id
        title
        details
        date
        urls
        director
        organized
        stash_ids { stash_id endpoint }
        studio     { id name }
        performers { id name }
        tags       { id name }
    }
}
"""

# remote_site_id is the stash-box ID returned alongside scraped metadata
SCRAPE_SINGLE_SCENE = """
mutation ScrapeSingleScene($source: ScraperSourceInput!, $input: ScrapeSingleSceneInput!) {
    scrapeSingleScene(source: $source, input: $input) {
        title
        details
        date
        urls
        director
        remote_site_id
        studio     { name stored_id remote_site_id }
        performers { name stored_id gender remote_site_id }
        tags       { name stored_id }
    }
}
"""

SCRAPE_SCENE_URL = """
mutation ScrapeSceneURL($url: String!) {
    scrapeSceneURL(url: $url) {
        title
        details
        date
        urls
        director
        studio     { name stored_id }
        performers { name stored_id gender }
        tags       { name stored_id }
    }
}
"""

FIND_STUDIOS = """
query FindStudios($q: String!) {
    findStudios(filter: { q: $q, per_page: 5 }) {
        studios { id name }
    }
}
"""

FIND_PERFORMERS = """
query FindPerformers($q: String!) {
    findPerformers(filter: { q: $q, per_page: 5 }) {
        performers { id name }
    }
}
"""

FIND_TAGS = """
query FindTags($q: String!) {
    findTags(filter: { q: $q, per_page: 5 }) {
        tags { id name }
    }
}
"""

STUDIO_CREATE = """
mutation StudioCreate($input: StudioCreateInput!) {
    studioCreate(input: $input) { id name }
}
"""

PERFORMER_CREATE = """
mutation PerformerCreate($input: PerformerCreateInput!) {
    performerCreate(input: $input) { id name }
}
"""

TAG_CREATE = """
mutation TagCreate($input: TagCreateInput!) {
    tagCreate(input: $input) { id name }
}
"""

SCENE_UPDATE = """
mutation SceneUpdate($input: SceneUpdateInput!) {
    sceneUpdate(input: $input) { id title }
}
"""

# ---------------------------------------------------------------------------
# GraphQL helper (compatible across stashapi versions)
# ---------------------------------------------------------------------------


def gql(stash, query, variables=None):
    for attr in ("call_GQL", "callGQL", "_callGraphQL"):
        fn = getattr(stash, attr, None)
        if callable(fn):
            return fn(query, variables)

    # Fallback: raw HTTP POST
    import urllib.request

    api_key = getattr(stash, "api_key", "") or getattr(stash, "_api_key", "")
    url = getattr(stash, "url", None) or getattr(stash, "_url", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["ApiKey"] = api_key
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(url, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    if body.get("errors"):
        raise Exception(body["errors"][0].get("message", str(body["errors"])))
    return body.get("data", {})


# ---------------------------------------------------------------------------
# Entity lookup / creation helpers  (used by "Scrape & Save" only)
# ---------------------------------------------------------------------------


def find_or_create_studio(stash, scraped):
    if not scraped:
        return None
    stored_id = scraped.get("stored_id")
    if stored_id:
        return stored_id

    name = (scraped.get("name") or "").strip()
    if not name:
        return None

    res = gql(stash, FIND_STUDIOS, {"q": name})
    for s in res.get("findStudios", {}).get("studios", []):
        if s["name"].lower() == name.lower():
            return s["id"]

    res = gql(stash, STUDIO_CREATE, {"input": {"name": name}})
    created = res.get("studioCreate")
    if created:
        log.info(f"Created studio: {name}")
        return created["id"]
    return None


def find_or_create_performer(stash, scraped):
    if not scraped:
        return None
    stored_id = scraped.get("stored_id")
    if stored_id:
        return stored_id

    name = (scraped.get("name") or "").strip()
    if not name:
        return None

    res = gql(stash, FIND_PERFORMERS, {"q": name})
    for p in res.get("findPerformers", {}).get("performers", []):
        if p["name"].lower() == name.lower():
            return p["id"]

    inp = {"name": name}
    if scraped.get("gender"):
        inp["gender"] = scraped["gender"]
    res = gql(stash, PERFORMER_CREATE, {"input": inp})
    created = res.get("performerCreate")
    if created:
        log.info(f"Created performer: {name}")
        return created["id"]
    return None


def find_or_create_tag(stash, scraped):
    if not scraped:
        return None
    stored_id = scraped.get("stored_id")
    if stored_id:
        return stored_id

    name = (scraped.get("name") or "").strip()
    if not name:
        return None

    res = gql(stash, FIND_TAGS, {"q": name})
    for t in res.get("findTags", {}).get("tags", []):
        if t["name"].lower() == name.lower():
            return t["id"]

    res = gql(stash, TAG_CREATE, {"input": {"name": name}})
    created = res.get("tagCreate")
    if created:
        log.info(f"Created tag: {name}")
        return created["id"]
    return None


# ---------------------------------------------------------------------------
# Save strategies
# ---------------------------------------------------------------------------


def save_match_only(stash, scene, scrape_data, stashbox_endpoint):
    """Save only the remote_site_id as a stash_id. No other fields are touched."""
    remote_site_id = (scrape_data or {}).get("remote_site_id")
    if not remote_site_id or not stashbox_endpoint:
        return False

    existing = scene.get("stash_ids") or []
    for sid in existing:
        if sid.get("endpoint") == stashbox_endpoint:
            log.debug(f"Scene {scene['id']} already matched to this endpoint — skipping")
            return False

    new_stash_ids = existing + [{"endpoint": stashbox_endpoint, "stash_id": remote_site_id}]
    gql(stash, SCENE_UPDATE, {"input": {"id": scene["id"], "stash_ids": new_stash_ids}})
    label = scene.get("title") or f"(id {scene['id']})"
    log.info(f"Matched: {label} → {remote_site_id}")
    return True


def save_full(stash, scene, scrape_data, overwrite):
    """Save all scraped metadata, creating any missing studios, performers, and tags."""
    if not scrape_data:
        return False

    update = {"id": scene["id"]}
    changed = False

    def _set(field, value):
        nonlocal changed
        if value and (overwrite or not scene.get(field)):
            update[field] = value
            changed = True

    _set("title", scrape_data.get("title"))
    _set("details", scrape_data.get("details"))
    _set("date", scrape_data.get("date"))
    _set("director", scrape_data.get("director"))

    scraped_urls = scrape_data.get("urls") or []
    if scraped_urls and (overwrite or not scene.get("urls")):
        update["urls"] = scraped_urls
        changed = True

    if scrape_data.get("studio") and (overwrite or not scene.get("studio")):
        studio_id = find_or_create_studio(stash, scrape_data["studio"])
        if studio_id:
            update["studio_id"] = studio_id
            changed = True

    scraped_performers = scrape_data.get("performers") or []
    if scraped_performers and (overwrite or not scene.get("performers")):
        performer_ids = [
            pid
            for p in scraped_performers
            if (pid := find_or_create_performer(stash, p))
        ]
        if performer_ids:
            update["performer_ids"] = performer_ids
            changed = True

    scraped_tags = scrape_data.get("tags") or []
    if scraped_tags and (overwrite or not scene.get("tags")):
        tag_ids = [
            tid
            for t in scraped_tags
            if (tid := find_or_create_tag(stash, t))
        ]
        if tag_ids:
            update["tag_ids"] = tag_ids
            changed = True

    if changed:
        gql(stash, SCENE_UPDATE, {"input": update})
        label = scene.get("title") or f"(id {scene['id']})"
        log.info(f"Saved: {label}")

    return changed


# ---------------------------------------------------------------------------
# Scraping strategies
# ---------------------------------------------------------------------------


def _scrape_with_source(stash, scene_id, source):
    res = gql(stash, SCRAPE_SINGLE_SCENE, {"source": source, "input": {"scene_id": scene_id}})
    return res.get("scrapeSingleScene")


def scrape_scene(stash, scene, stashbox_endpoint, scraper_id):
    """Try all applicable scraping strategies and return the first result."""
    scene_id = scene["id"]

    # 1. Stash-box (works with or without an existing stash_id — the scraper decides)
    if stashbox_endpoint:
        try:
            data = _scrape_with_source(stash, scene_id, {"stash_box_endpoint": stashbox_endpoint})
            if data:
                return data
        except Exception as exc:
            log.warning(f"Stash-box scrape failed for scene {scene_id}: {exc}")

    # 2. Specific scraper by ID
    if scraper_id:
        try:
            data = _scrape_with_source(stash, scene_id, {"scraper_id": scraper_id})
            if data:
                return data
        except Exception as exc:
            log.warning(f"Scraper '{scraper_id}' failed for scene {scene_id}: {exc}")

    # 3. URL auto-detection
    for url in scene.get("urls") or []:
        if not url:
            continue
        try:
            res = gql(stash, SCRAPE_SCENE_URL, {"url": url})
            data = res.get("scrapeSceneURL")
            if data:
                return data
        except Exception as exc:
            log.debug(f"URL scrape failed for {url}: {exc}")

    return None


# ---------------------------------------------------------------------------
# Scene retrieval
# ---------------------------------------------------------------------------


def fetch_all_scenes(stash, page_size=100):
    all_scenes = []
    page = 1
    while True:
        res = gql(stash, FIND_SCENES_PAGE, {
            "filter": {"per_page": page_size, "page": page},
            "scene_filter": {},
        })
        data = res.get("findScenes", {})
        scenes = data.get("scenes", [])
        all_scenes.extend(scenes)
        if len(all_scenes) >= data.get("count", 0) or not scenes:
            break
        page += 1
    return all_scenes


def fetch_scene_by_id(stash, scene_id):
    res = gql(stash, FIND_SCENE_BY_ID, {"id": scene_id})
    return res.get("findScene")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def resolve_stashbox_endpoint(stash, configured):
    if configured and configured.strip():
        return configured.strip()
    try:
        res = gql(stash, PLUGIN_AND_STASHBOX_CONFIG)
        boxes = res.get("configuration", {}).get("general", {}).get("stashBoxes", [])
        if boxes:
            name = boxes[0].get("name") or boxes[0].get("endpoint", "")
            log.info(f"Using stash-box: {name}")
            return boxes[0].get("endpoint")
    except Exception as exc:
        log.warning(f"Could not fetch stash-box config: {exc}")
    return None


def run(stash, settings, mode, single_scene_id=None, scene_ids=None):
    stashbox_endpoint = resolve_stashbox_endpoint(stash, settings.get("stashbox_endpoint", ""))
    scraper_id = (settings.get("scraper_id") or "").strip() or None
    overwrite = settings.get("overwrite_data", False)

    match_only = mode in ("match_all", "match_scene", "match_selected")

    if scene_ids:
        scenes = [s for s in (fetch_scene_by_id(stash, sid) for sid in scene_ids) if s]
        log.info(f"Processing {len(scenes)} selected scene(s)…")
    elif single_scene_id:
        scene = fetch_scene_by_id(stash, single_scene_id)
        if not scene:
            log.error(f"Scene {single_scene_id} not found")
            return
        scenes = [scene]
    else:
        log.info("Fetching all scenes…")
        scenes = fetch_all_scenes(stash)

    total = len(scenes)
    log.info(f"{'Matching' if match_only else 'Scraping'} {total} scene(s)…")

    updated = skipped = errors = 0
    for i, scene in enumerate(scenes):
        log.progress(i / total if total else 1)
        try:
            scrape_data = scrape_scene(stash, scene, stashbox_endpoint, scraper_id)
            if scrape_data:
                if match_only:
                    saved = save_match_only(stash, scene, scrape_data, stashbox_endpoint)
                else:
                    saved = save_full(stash, scene, scrape_data, overwrite)
                if saved:
                    updated += 1
                else:
                    skipped += 1
            else:
                log.debug(f"No scrape data for scene {scene['id']}")
                skipped += 1
        except Exception as exc:
            log.error(f"Error on scene {scene['id']}: {exc}")
            errors += 1

        # Small delay to avoid hammering scrapers on large runs
        if not (single_scene_id and total == 1):
            time.sleep(0.1)

    log.progress(1.0)
    log.info(f"Done — {updated} saved, {skipped} skipped, {errors} error(s)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    raw = sys.stdin.read()
    json_input = json.loads(raw)

    mode = json_input.get("args", {}).get("mode", "")
    scene_id = json_input.get("args", {}).get("scene_id")
    stash = StashInterface(json_input["server_connection"])

    res = gql(stash, PLUGIN_AND_STASHBOX_CONFIG)
    settings = (
        res.get("configuration", {})
        .get("plugins", {})
        .get(PLUGIN_ID, {})
    )

    log.info(f"stash-scrape starting (mode={mode})")

    if mode in ("match_all", "scrape_all"):
        run(stash, settings, mode)
    elif mode in ("match_scene", "scrape_scene"):
        if not scene_id:
            log.error(f"{mode} requires a scene_id argument")
            return
        run(stash, settings, mode, single_scene_id=scene_id)
    elif mode in ("match_selected", "scrape_selected"):
        raw = json_input.get("args", {}).get("scene_ids", [])
        # Accept either a JSON array or a comma-separated string
        if isinstance(raw, str):
            raw = [s.strip() for s in raw.split(",") if s.strip()]
        if not raw:
            log.error(f"{mode} requires a scene_ids argument")
            return
        run(stash, settings, mode, scene_ids=raw)
    else:
        log.error(f"Unknown mode: '{mode}'")


if __name__ == "__main__":
    main()
