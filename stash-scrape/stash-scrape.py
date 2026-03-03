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
            rating100
            director
            organized
            stash_ids { stash_id endpoint }
            studio  { id name }
            performers { id name }
            tags    { id name }
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
        rating100
        director
        organized
        stash_ids { stash_id endpoint }
        studio  { id name }
        performers { id name }
        tags    { id name }
    }
}
"""

LIST_SCENE_SCRAPERS = """
query ListSceneScrapers {
    listScrapers(types: [SCENE]) {
        id
        name
        scene { supported_scrapes }
    }
}
"""

SCRAPE_SINGLE_SCENE = """
mutation ScrapeSingleScene($source: ScrapeSource!, $input: ScrapeSingleSceneInput!) {
    scrapeSingleScene(source: $source, input: $input) {
        title
        details
        date
        urls
        director
        studio    { name stored_id }
        performers { name stored_id gender }
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
        studio    { name stored_id }
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
# Entity lookup / creation helpers
# ---------------------------------------------------------------------------


def find_or_create_studio(stash, scraped, create):
    """Return a Stash studio ID for the scraped studio, creating one if allowed."""
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

    if create:
        res = gql(stash, STUDIO_CREATE, {"input": {"name": name}})
        created = res.get("studioCreate")
        if created:
            log.info(f"Created studio: {name}")
            return created["id"]
    else:
        log.debug(f"Studio '{name}' not found — creation disabled, skipping")
    return None


def find_or_create_performer(stash, scraped, create):
    """Return a Stash performer ID for the scraped performer, creating one if allowed."""
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

    if create:
        inp = {"name": name}
        gender = scraped.get("gender")
        if gender:
            inp["gender"] = gender
        res = gql(stash, PERFORMER_CREATE, {"input": inp})
        created = res.get("performerCreate")
        if created:
            log.info(f"Created performer: {name}")
            return created["id"]
    else:
        log.debug(f"Performer '{name}' not found — creation disabled, skipping")
    return None


def find_or_create_tag(stash, scraped, create):
    """Return a Stash tag ID for the scraped tag, creating one if allowed."""
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

    if create:
        res = gql(stash, TAG_CREATE, {"input": {"name": name}})
        created = res.get("tagCreate")
        if created:
            log.info(f"Created tag: {name}")
            return created["id"]
    else:
        log.debug(f"Tag '{name}' not found — creation disabled, skipping")
    return None


# ---------------------------------------------------------------------------
# Applying scrape results to a scene
# ---------------------------------------------------------------------------


def apply_scrape_result(stash, scene, scrape_data, settings):
    """Merge scrape_data into the scene, respecting per-attribute creation settings.

    Returns True if the scene was updated.
    """
    if not scrape_data:
        return False

    overwrite = settings.get("overwrite_data", False)
    create_studio = settings.get("create_studio", False)
    create_performers = settings.get("create_performers", False)
    create_tags = settings.get("create_tags", False)

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

    # Studio
    if scrape_data.get("studio") and (overwrite or not scene.get("studio")):
        studio_id = find_or_create_studio(stash, scrape_data["studio"], create_studio)
        if studio_id:
            update["studio_id"] = studio_id
            changed = True

    # Performers
    scraped_performers = scrape_data.get("performers") or []
    if scraped_performers and (overwrite or not scene.get("performers")):
        performer_ids = [
            pid
            for p in scraped_performers
            if (pid := find_or_create_performer(stash, p, create_performers))
        ]
        if performer_ids:
            update["performer_ids"] = performer_ids
            changed = True

    # Tags
    scraped_tags = scrape_data.get("tags") or []
    if scraped_tags and (overwrite or not scene.get("tags")):
        tag_ids = [
            tid
            for t in scraped_tags
            if (tid := find_or_create_tag(stash, t, create_tags))
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

    # 1. Stash-box by stash_id match
    if stashbox_endpoint:
        for sid in scene.get("stash_ids") or []:
            if sid.get("endpoint") == stashbox_endpoint:
                try:
                    data = _scrape_with_source(
                        stash, scene_id, {"stash_box_endpoint": stashbox_endpoint}
                    )
                    if data:
                        return data
                except Exception as exc:
                    log.warning(f"Stash-box scrape failed for scene {scene_id}: {exc}")
                break

    # 2. Stash-box query even without a stash_id (scraper tries a title/URL search)
    if stashbox_endpoint and not scene.get("stash_ids"):
        try:
            data = _scrape_with_source(
                stash, scene_id, {"stash_box_endpoint": stashbox_endpoint}
            )
            if data:
                return data
        except Exception as exc:
            log.debug(f"Stash-box query scrape skipped for scene {scene_id}: {exc}")

    # 3. Specific scraper by ID
    if scraper_id:
        try:
            data = _scrape_with_source(stash, scene_id, {"scraper_id": scraper_id})
            if data:
                return data
        except Exception as exc:
            log.warning(f"Scraper '{scraper_id}' failed for scene {scene_id}: {exc}")

    # 4. URL auto-detection (scrapeSceneURL)
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


def fetch_all_scenes(stash, scene_filter=None, page_size=100):
    all_scenes = []
    page = 1
    while True:
        res = gql(stash, FIND_SCENES_PAGE, {
            "filter": {"per_page": page_size, "page": page},
            "scene_filter": scene_filter or {},
        })
        data = res.get("findScenes", {})
        scenes = data.get("scenes", [])
        all_scenes.extend(scenes)
        total = data.get("count", 0)
        if len(all_scenes) >= total or not scenes:
            break
        page += 1
    return all_scenes


# ---------------------------------------------------------------------------
# Main scrape runner
# ---------------------------------------------------------------------------


def resolve_stashbox_endpoint(stash, configured_endpoint, use_stashbox):
    """Return the stash-box endpoint to use, or None if not applicable."""
    if not use_stashbox and not configured_endpoint:
        return None
    if configured_endpoint:
        return configured_endpoint.strip() or None

    # Auto-detect: use first stash-box configured in Stash
    try:
        res = gql(stash, PLUGIN_AND_STASHBOX_CONFIG)
        boxes = res.get("configuration", {}).get("general", {}).get("stashBoxes", [])
        if boxes:
            name = boxes[0].get("name", boxes[0].get("endpoint", ""))
            log.info(f"Using stash-box: {name}")
            return boxes[0].get("endpoint")
    except Exception as exc:
        log.warning(f"Could not fetch stash-box config: {exc}")
    return None


def run_scrape(stash, settings, scene_filter=None, single_scene_id=None):
    use_stashbox = settings.get("scrape_with_stashbox", True)
    stashbox_endpoint = resolve_stashbox_endpoint(
        stash,
        settings.get("stashbox_endpoint", ""),
        use_stashbox,
    )
    scraper_id = (settings.get("scraper_id") or "").strip() or None

    # Collect scenes
    if single_scene_id:
        res = gql(stash, FIND_SCENE_BY_ID, {"id": single_scene_id})
        scene = res.get("findScene")
        if not scene:
            log.error(f"Scene {single_scene_id} not found")
            return
        scenes = [scene]
    else:
        log.info("Fetching scenes…")
        scenes = fetch_all_scenes(stash, scene_filter)
        if scene_filter == "missing":
            scenes = [
                s for s in scenes
                if not (s.get("title") and s.get("studio"))
            ]

    total = len(scenes)
    log.info(f"Scraping {total} scene(s)…")

    updated = skipped = errors = 0
    for i, scene in enumerate(scenes):
        log.progress(i / total if total else 1)
        try:
            scrape_data = scrape_scene(stash, scene, stashbox_endpoint, scraper_id)
            if scrape_data:
                if apply_scrape_result(stash, scene, scrape_data, settings):
                    updated += 1
                else:
                    skipped += 1
            else:
                log.debug(f"No scrape data for scene {scene['id']}")
                skipped += 1
        except Exception as exc:
            log.error(f"Error scraping scene {scene['id']}: {exc}")
            errors += 1

        # Small delay to avoid hammering scrapers on large runs
        if not single_scene_id:
            time.sleep(0.1)

    log.progress(1.0)
    log.info(
        f"Done — {updated} updated, {skipped} skipped, {errors} error(s)"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    raw = sys.stdin.read()
    json_input = json.loads(raw)

    mode = json_input.get("args", {}).get("mode", "")
    scene_id = json_input.get("args", {}).get("scene_id")
    stash = StashInterface(json_input["server_connection"])

    # Load plugin settings
    res = gql(stash, PLUGIN_AND_STASHBOX_CONFIG)
    settings = (
        res.get("configuration", {})
        .get("plugins", {})
        .get(PLUGIN_ID, {})
    )

    log.info(f"stash-scrape starting (mode={mode})")

    if mode == "scrape_all":
        run_scrape(stash, settings)
    elif mode == "scrape_missing":
        run_scrape(stash, settings, scene_filter="missing")
    elif mode == "scrape_scene":
        if not scene_id:
            log.error("scrape_scene requires a scene_id argument")
            return
        run_scrape(stash, settings, single_scene_id=scene_id)
    else:
        log.error(f"Unknown mode: '{mode}'")


if __name__ == "__main__":
    main()
