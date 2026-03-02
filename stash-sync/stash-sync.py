import json
import os
import sys
import shutil
import time
import base64
from urllib.parse import urlparse

# Support vendored dependencies installed via pip --target lib/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))

import requests  # noqa: E402
import stashapi.log as log  # noqa: E402
from stashapi.stashapp import StashInterface  # noqa: E402

DEFAULT_TRANSFER_TAG = "stash-sync: Transfer"
SCAN_TIMEOUT = 120
SCENE_FIND_MAX_ATTEMPTS = 8

# ---------------------------------------------------------------------------
# GraphQL fragments & queries
# ---------------------------------------------------------------------------

SCENE_FRAGMENT = """
fragment FullScene on Scene {
    id
    title
    code
    details
    director
    urls
    date
    rating100
    organized
    stash_ids { endpoint stash_id }
    files {
        id
        path
        basename
        fingerprints { type value }
    }
    performers {
        id
        name
        disambiguation
        gender
        stash_ids { endpoint stash_id }
        image_path
    }
    tags {
        id
        name
    }
    studio {
        id
        name
        stash_ids { endpoint stash_id }
        image_path
    }
    groups {
        group { id name }
        scene_index
    }
    scene_markers {
        id
        title
        seconds
        primary_tag { id name }
        tags { id name }
    }
    paths { screenshot }
}
"""

FIND_FULL_SCENE = (
    "query FindFullScene($id: ID!) { findScene(id: $id) { ...FullScene } }"
    + SCENE_FRAGMENT
)

FIND_SCENE_BY_HASH = """
query FindSceneByHash($input: SceneHashInput!) {
    findSceneByHash(input: $input) { id }
}
"""

FIND_SCENES_BY_PATH = """
query FindScenesByPath($filter: FindFilterType, $scene_filter: SceneFilterType) {
    findScenes(filter: $filter, scene_filter: $scene_filter) {
        scenes { id files { path } }
    }
}
"""

REMOTE_LIBRARY_PATHS = """
query {
    configuration {
        general { stashes { path } }
    }
}
"""

TRIGGER_SCAN = """
mutation MetadataScan($input: ScanMetadataInput!) {
    metadataScan(input: $input)
}
"""

FIND_JOB = """
query FindJob($input: FindJobInput!) {
    findJob(input: $input) { id status progress }
}
"""

SCENE_UPDATE = """
mutation SceneUpdate($input: SceneUpdateInput!) {
    sceneUpdate(input: $input) { id }
}
"""

SCENE_DESTROY = """
mutation SceneDestroy($input: SceneDestroyInput!) {
    sceneDestroy(input: $input)
}
"""

MARKER_CREATE = """
mutation SceneMarkerCreate($input: SceneMarkerCreateInput!) {
    sceneMarkerCreate(input: $input) { id }
}
"""

PLUGIN_CONFIG = """
query Configuration {
    configuration { plugins }
}
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def gql(stash, query, variables=None):
    """Execute a GraphQL query against a StashInterface instance."""
    for attr in ("call_GQL", "callGQL", "_callGraphQL"):
        fn = getattr(stash, attr, None)
        if callable(fn):
            return fn(query, variables)

    # Fallback: raw HTTP request
    headers = {"Content-Type": "application/json"}
    api_key = getattr(stash, "api_key", "") or getattr(stash, "_api_key", "")
    if api_key:
        headers["ApiKey"] = api_key
    url = getattr(stash, "url", None) or getattr(stash, "_url", "")
    resp = requests.post(
        url,
        json={"query": query, "variables": variables or {}},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise Exception(body["errors"][0].get("message", str(body["errors"])))
    return body.get("data", {})


def stash_base_url(stash):
    """Derive the HTTP base URL for a Stash instance."""
    url = stash.url
    if url.endswith("/graphql"):
        url = url[:-8]
    return url.replace("0.0.0.0", "localhost")


def fetch_image_b64(stash, image_url):
    """Download an image from a Stash instance and return a data-URI string."""
    if not image_url:
        return None
    try:
        if image_url.startswith("/"):
            image_url = stash_base_url(stash) + image_url
        headers = {}
        if stash.api_key:
            headers["ApiKey"] = stash.api_key
        resp = requests.get(image_url, headers=headers, timeout=15)
        if resp.status_code == 200:
            ct = resp.headers.get("Content-Type", "image/jpeg")
            b64 = base64.b64encode(resp.content).decode()
            return f"data:{ct};base64,{b64}"
    except Exception as exc:
        log.warning(f"Image fetch failed ({image_url}): {exc}")
    return None


def wait_for_job(stash, job_id, timeout=SCAN_TIMEOUT, log_prefix="Remote"):
    """Block until a Stash job finishes or timeout is reached."""
    if not job_id:
        log.info(f"[{log_prefix}] No job ID returned from scan; waiting 3s for scan to settle")
        time.sleep(3)
        return
    log.info(f"[{log_prefix}] Waiting for job {job_id} (timeout={timeout}s)")
    deadline = time.time() + timeout
    last_progress = None
    poll_count = 0
    while time.time() < deadline:
        time.sleep(2)
        poll_count += 1
        result = gql(stash, FIND_JOB, {"input": {"id": job_id}})
        job = result.get("findJob")
        if not job:
            log.info(f"[{log_prefix}] Job {job_id} no longer in queue (assume finished)")
            return
        status = job.get("status")
        progress = job.get("progress")
        if progress != last_progress or poll_count == 1:
            log.info(f"[{log_prefix}] Job {job_id} status={status} progress={progress}")
            last_progress = progress
        if status in ("FINISHED", "CANCELLED"):
            log.info(f"[{log_prefix}] Job {job_id} completed with status={status}")
            return
        if status == "FAILED":
            raise RuntimeError(f"Job {job_id} failed on remote instance")
    log.warning(f"[{log_prefix}] Job {job_id} did not complete within {timeout}s")


def ensure_tag(stash, tag_name):
    """Return the ID of *tag_name*, creating it if it doesn't exist."""
    for tag in stash.find_tags(q=tag_name) or []:
        if tag["name"].lower() == tag_name.lower():
            return tag["id"]
    result = stash.create_tag({"name": tag_name})
    if result:
        log.info(f"Created transfer tag: {tag_name}")
        return result["id"]
    raise RuntimeError(f"Could not create tag '{tag_name}'")


def find_tagged_scenes(stash, tag_id):
    """Return every scene carrying *tag_id*."""
    page, per_page, all_scenes = 1, 100, []
    while True:
        batch = stash.find_scenes(
            f={"tags": {"value": [tag_id], "modifier": "INCLUDES", "depth": 0}},
            filter={"page": page, "per_page": per_page},
        )
        if not batch:
            break
        all_scenes.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return all_scenes


# ---------------------------------------------------------------------------
# Entity resolver with cross-batch caching
# ---------------------------------------------------------------------------


class EntityResolver:
    """Matches or creates performers/tags/studios/groups on the remote
    instance, caching results so repeated lookups are free."""

    def __init__(self, source, remote):
        self.source = source
        self.remote = remote
        self._performers = {}
        self._tags = {}
        self._studios = {}
        self._groups = {}

    # -- performers ---------------------------------------------------------

    def resolve_performer(self, performer):
        name = performer["name"]
        cache_key = f"{name}||{performer.get('disambiguation', '')}"
        if cache_key in self._performers:
            return self._performers[cache_key]

        remote_id = self._match_performer_by_stash_id(performer)
        if not remote_id:
            remote_id = self._match_performer_by_name(performer)
        if not remote_id:
            remote_id = self._create_performer(performer)

        if remote_id:
            self._performers[cache_key] = remote_id
        return remote_id

    def _match_performer_by_stash_id(self, performer):
        for sid in performer.get("stash_ids") or []:
            hits = self.remote.find_performers(f={
                "stash_id_endpoint": {
                    "endpoint": sid["endpoint"],
                    "stash_id": sid["stash_id"],
                    "modifier": "EQUALS",
                }
            })
            if hits:
                return hits[0]["id"]
        return None

    def _match_performer_by_name(self, performer):
        name = performer["name"]
        dis = (performer.get("disambiguation") or "").lower()
        for p in self.remote.find_performers(q=name) or []:
            if p["name"].lower() == name.lower():
                p_dis = (p.get("disambiguation") or "").lower()
                if dis == p_dis:
                    return p["id"]
        return None

    def _create_performer(self, performer):
        inp = {"name": performer["name"]}
        if performer.get("stash_ids"):
            inp["stash_ids"] = performer["stash_ids"]
        if performer.get("gender"):
            inp["gender"] = performer["gender"]
        if performer.get("disambiguation"):
            inp["disambiguation"] = performer["disambiguation"]
        if performer.get("image_path"):
            img = fetch_image_b64(self.source, performer["image_path"])
            if img:
                inp["image"] = img
        result = self.remote.create_performer(inp)
        if result:
            log.info(f"Created performer on destination: {performer['name']} -> {result['id']}")
            return result["id"]
        return None

    # -- tags ---------------------------------------------------------------

    def resolve_tag(self, tag):
        name = tag["name"]
        if name in self._tags:
            return self._tags[name]

        for t in self.remote.find_tags(q=name) or []:
            if t["name"].lower() == name.lower():
                self._tags[name] = t["id"]
                return t["id"]

        result = self.remote.create_tag({"name": name})
        if result:
            log.info(f"Created tag on destination: {name} -> {result['id']}")
            self._tags[name] = result["id"]
            return result["id"]
        return None

    # -- studios ------------------------------------------------------------

    def resolve_studio(self, studio):
        name = studio["name"]
        if name in self._studios:
            return self._studios[name]

        remote_id = self._match_studio_by_stash_id(studio)
        if not remote_id:
            remote_id = self._match_studio_by_name(studio)
        if not remote_id:
            remote_id = self._create_studio(studio)

        if remote_id:
            self._studios[name] = remote_id
        return remote_id

    def _match_studio_by_stash_id(self, studio):
        for sid in studio.get("stash_ids") or []:
            hits = self.remote.find_studios(f={
                "stash_id_endpoint": {
                    "endpoint": sid["endpoint"],
                    "stash_id": sid["stash_id"],
                    "modifier": "EQUALS",
                }
            })
            if hits:
                return hits[0]["id"]
        return None

    def _match_studio_by_name(self, studio):
        for s in self.remote.find_studios(q=studio["name"]) or []:
            if s["name"].lower() == studio["name"].lower():
                return s["id"]
        return None

    def _create_studio(self, studio):
        inp = {"name": studio["name"]}
        if studio.get("stash_ids"):
            inp["stash_ids"] = studio["stash_ids"]
        if studio.get("image_path"):
            img = fetch_image_b64(self.source, studio["image_path"])
            if img:
                inp["image"] = img
        result = self.remote.create_studio(inp)
        if result:
            log.info(f"Created studio on destination: {studio['name']} -> {result['id']}")
            return result["id"]
        return None

    # -- groups -------------------------------------------------------------

    def resolve_group(self, group_entry):
        group = group_entry["group"]
        name = group["name"]
        if name in self._groups:
            return self._groups[name]

        for g in self.remote.find_groups(q=name) or []:
            if g["name"].lower() == name.lower():
                self._groups[name] = g["id"]
                return g["id"]

        result = self.remote.create_group({"name": name})
        if result:
            log.info(f"Created group on destination: {name} -> {result['id']}")
            self._groups[name] = result["id"]
            return result["id"]
        return None


# ---------------------------------------------------------------------------
# Single-scene transfer
# ---------------------------------------------------------------------------


def transfer_scene(scene_id, source, remote, resolver, dest_path, tag_name, remote_name="Remote"):
    """Move one scene from *source* to *remote*, preserving all metadata."""
    r = remote_name

    log.info(f"=== Transfer starting: scene_id={scene_id} ===")

    # 1. Full scene query (source)
    log.info(f"[Source] Step 1/10: Fetching full scene {scene_id}")
    data = gql(source, FIND_FULL_SCENE, {"id": str(scene_id)})
    scene = data.get("findScene")
    if not scene:
        raise ValueError(f"Scene {scene_id} not found on source instance")

    title = scene.get("title") or f"Scene {scene_id}"
    log.info(f"[Source] Step 1/10: Got scene title={title!r}")

    if not scene.get("files"):
        raise ValueError(f"Scene {scene_id} has no associated files")
    primary_file = scene["files"][0]
    source_path = primary_file["path"]
    filename = primary_file.get("basename") or os.path.basename(source_path)

    oshash = None
    for fp in primary_file.get("fingerprints", []):
        if fp["type"] == "oshash":
            oshash = fp["value"]
            break
    if not oshash:
        raise ValueError(f"Scene {scene_id} has no oshash fingerprint")
    log.info(f"[Source] Step 1/10: File path={source_path!r} filename={filename!r} oshash={oshash}")

    # 2. Fetch cover (source)
    log.info(f"[Source] Step 2/10: Fetching cover image")
    cover_b64 = fetch_image_b64(source, (scene.get("paths") or {}).get("screenshot"))
    log.info(f"[Source] Step 2/10: Cover fetched: {bool(cover_b64)}")

    # 3. Resolve entities on destination (remote lookups / creates)
    log.info(f"[{r}] Step 3/10: Resolving performers, tags, studio, groups on destination")
    performer_ids = []
    for p in scene.get("performers") or []:
        pid = resolver.resolve_performer(p)
        if pid:
            performer_ids.append(pid)
    log.info(f"[{r}] Step 3/10: Resolved {len(performer_ids)} performers")

    tag_ids = []
    for t in scene.get("tags") or []:
        if t["name"].lower() == tag_name.lower():
            continue
        tid = resolver.resolve_tag(t)
        if tid:
            tag_ids.append(tid)
    log.info(f"[{r}] Step 3/10: Resolved {len(tag_ids)} tags (excluding transfer tag)")

    studio_id = None
    if scene.get("studio"):
        studio_id = resolver.resolve_studio(scene["studio"])
    log.info(f"[{r}] Step 3/10: Studio resolved: {studio_id is not None}")

    groups = []
    for g in scene.get("groups") or []:
        gid = resolver.resolve_group(g)
        if gid:
            groups.append({"group_id": gid, "scene_index": g.get("scene_index")})
    log.info(f"[{r}] Step 3/10: Resolved {len(groups)} group entries")

    # 4. Move the file (filesystem — file in "purgatory" until remote scan + metadata done)
    dest_file = os.path.join(dest_path, filename)
    if os.path.exists(dest_file):
        raise FileExistsError(f"Destination already exists: {dest_file}")
    log.info(f"[Filesystem] Step 4/10: Ensuring destination dir exists: {dest_path}")
    os.makedirs(dest_path, exist_ok=True)
    log.info(f"[Filesystem] Step 4/10: Moving file: {source_path} -> {dest_file}")
    shutil.move(source_path, dest_file)
    log.info(f"[Filesystem] Step 4/10: Move complete; file now only at destination path")

    try:
        # 5. Trigger scan on destination (remote)
        log.info(f"[{r}] Step 5/10: Triggering metadata scan for path: {dest_file}")
        scan_result = gql(remote, TRIGGER_SCAN, {
            "input": {"paths": [dest_file]}
        })
        job_id = scan_result.get("metadataScan")
        log.info(f"[{r}] Step 5/10: Scan triggered; job_id={job_id}")
        wait_for_job(remote, job_id, log_prefix=r)

        # 6. Find the new scene on the destination (remote)
        log.info(f"[{r}] Step 6/10: Looking up scene by destination path (and oshash fallback)")
        new_scene_id = None
        for attempt in range(SCENE_FIND_MAX_ATTEMPTS):
            wait = min(2 ** attempt, 15)
            if attempt > 0:
                log.info(f"[{r}] Step 6/10: Attempt {attempt + 1}/{SCENE_FIND_MAX_ATTEMPTS}: waiting {wait}s then re-querying")
            time.sleep(wait)

            result = gql(remote, FIND_SCENES_BY_PATH, {
                "filter": {"per_page": 5},
                "scene_filter": {
                    "path": {"value": dest_file, "modifier": "EQUALS"}
                },
            })
            scenes_found = (result.get("findScenes") or {}).get("scenes") or []
            log.info(f"[{r}] Step 6/10: findScenes(path={dest_file!r}) returned {len(scenes_found)} scene(s)")
            for s in scenes_found:
                for f in s.get("files") or []:
                    if f.get("path") == dest_file:
                        new_scene_id = s["id"]
                        log.info(f"[{r}] Step 6/10: Found scene by path: id={new_scene_id} path={f.get('path')!r}")
                        break
                if new_scene_id:
                    break

            if not new_scene_id:
                result = gql(remote, FIND_SCENE_BY_HASH, {
                    "input": {"oshash": oshash}
                })
                found = result.get("findSceneByHash")
                if found:
                    new_scene_id = found["id"]
                    log.info(f"[{r}] Step 6/10: Found scene by oshash fallback: id={new_scene_id}")

            if new_scene_id:
                break
            log.info(f"[{r}] Step 6/10: Scene not yet visible on destination (attempt {attempt + 1})")

        if not new_scene_id:
            raise TimeoutError(
                f"Scene not found on destination after scan. "
                f"oshash={oshash}, file={dest_file}"
            )

        log.info(f"[{r}] Step 6/10: Using destination scene_id={new_scene_id}")

        # 7. Apply metadata (remote)
        log.info(f"[{r}] Step 7/10: Applying metadata (title, performers, tags, studio, cover, etc.) to scene {new_scene_id}")
        update = {
            "id": new_scene_id,
            "title": scene.get("title"),
            "code": scene.get("code"),
            "details": scene.get("details"),
            "director": scene.get("director"),
            "urls": scene.get("urls") or [],
            "date": scene.get("date"),
            "rating100": scene.get("rating100"),
            "organized": scene.get("organized", False),
            "performer_ids": performer_ids,
            "tag_ids": tag_ids,
            "stash_ids": [
                {"endpoint": s["endpoint"], "stash_id": s["stash_id"]}
                for s in (scene.get("stash_ids") or [])
            ],
        }
        if studio_id:
            update["studio_id"] = studio_id
        if groups:
            update["groups"] = groups
        if cover_b64:
            update["cover_image"] = cover_b64

        # Strip None values so we don't accidentally null fields
        update = {k: v for k, v in update.items() if v is not None}

        gql(remote, SCENE_UPDATE, {"input": update})
        log.info(f"[{r}] Step 7/10: sceneUpdate completed for scene {new_scene_id}")

        # 8. Recreate scene markers (remote)
        markers = scene.get("scene_markers") or []
        log.info(f"[{r}] Step 8/10: Creating {len(markers)} scene marker(s)")
        for marker in markers:
            primary_tag = marker.get("primary_tag")
            if not primary_tag:
                continue
            ptag_id = resolver.resolve_tag(primary_tag)
            if not ptag_id:
                continue
            mtag_ids = []
            for mt in marker.get("tags") or []:
                mtid = resolver.resolve_tag(mt)
                if mtid:
                    mtag_ids.append(mtid)
            gql(remote, MARKER_CREATE, {"input": {
                "scene_id": new_scene_id,
                "title": marker.get("title", ""),
                "seconds": marker["seconds"],
                "primary_tag_id": ptag_id,
                "tag_ids": mtag_ids,
            }})
        log.info(f"[{r}] Step 8/10: Scene markers created")

    except Exception:
        log.error(f"Metadata failed after file move. File is at: {dest_file}")
        raise

    # 9. Strip the transfer tag (source)
    log.info(f"[Source] Step 9/10: Stripping transfer tag from scene {scene_id}")
    remaining_tags = [
        t["id"] for t in (scene.get("tags") or [])
        if t["name"].lower() != tag_name.lower()
    ]
    try:
        gql(source, SCENE_UPDATE, {"input": {
            "id": str(scene_id),
            "tag_ids": remaining_tags,
        }})
        log.info(f"[Source] Step 9/10: Transfer tag stripped")
    except Exception as exc:
        log.warning(f"Could not strip transfer tag from source scene {scene_id}: {exc}")

    # 10. Cleanup source (source)
    log.info(f"[Source] Step 10/10: Destroying scene record {scene_id} (file already moved)")
    try:
        gql(source, SCENE_DESTROY, {"input": {
            "id": str(scene_id),
            "delete_file": False,
            "delete_generated": True,
        }})
        log.info(f"[Source] Step 10/10: Scene {scene_id} deleted from source")
    except Exception as exc:
        log.error(
            f"Failed to delete source scene {scene_id}: {exc}. "
            "You may need to remove it manually."
        )

    log.info(f"=== Transfer complete: {title} ({filename}) ===")

    except Exception:
        log.error(f"Metadata failed after file move. File is at: {dest_file}")
        raise

    # 9. Strip the transfer tag so the scene won't be re-processed if
    #    the destroy below fails
    remaining_tags = [
        t["id"] for t in (scene.get("tags") or [])
        if t["name"].lower() != tag_name.lower()
    ]
    try:
        gql(source, SCENE_UPDATE, {"input": {
            "id": str(scene_id),
            "tag_ids": remaining_tags,
        }})
    except Exception as exc:
        log.warning(f"Could not strip transfer tag from source scene {scene_id}: {exc}")

    # 10. Cleanup source — file is already moved so just remove the DB entry
    log.info(f"Deleting source scene {scene_id}...")
    try:
        gql(source, SCENE_DESTROY, {"input": {
            "id": str(scene_id),
            "delete_file": False,
            "delete_generated": True,
        }})
        log.info(f"Source scene {scene_id} deleted")
    except Exception as exc:
        log.error(
            f"Failed to delete source scene {scene_id}: {exc}. "
            "You may need to remove it manually."
        )

    log.info(f"Done: {title} ({filename})")


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def dry_run(source, remote, tag_id, tag_name):
    scenes = find_tagged_scenes(source, tag_id)
    if not scenes:
        log.info("No scenes found with the transfer tag.")
        return

    log.info(f"=== DRY RUN: {len(scenes)} scene(s) tagged '{tag_name}' ===")

    total_bytes = 0
    performers, tags, studios, groups = set(), set(), set(), set()

    for scene in scenes:
        data = gql(source, FIND_FULL_SCENE, {"id": str(scene["id"])})
        full = data.get("findScene")
        if not full:
            continue

        title = full.get("title") or f"Scene {full['id']}"

        for file_entry in full.get("files") or []:
            for fp in file_entry.get("fingerprints") or []:
                if fp["type"] == "size":
                    try:
                        total_bytes += int(fp["value"])
                    except (ValueError, TypeError):
                        pass

        for p in full.get("performers") or []:
            performers.add(p["name"])
        for t in full.get("tags") or []:
            if t["name"].lower() != tag_name.lower():
                tags.add(t["name"])
        if full.get("studio"):
            studios.add(full["studio"]["name"])
        for g in full.get("groups") or []:
            groups.add(g["group"]["name"])

        log.info(f"  - {title}")

    log.info("=== Summary ===")
    log.info(f"Scenes:     {len(scenes)}")
    log.info(f"Total size: {total_bytes / (1024 ** 3):.2f} GB")
    log.info(f"Performers: {len(performers)} unique")
    log.info(f"Tags:       {len(tags)} unique")
    log.info(f"Studios:    {len(studios)} unique")
    log.info(f"Groups:     {len(groups)} unique")


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------


def test_connection(source, remote, remote_name, remote_url, dest_path, tag_name):
    """Validate that all settings are correct and both instances can talk."""
    errors = []

    # 1. Remote API
    log.info(f"[1/5] Remote instance ({remote_name} @ {remote_url})")
    try:
        ver = gql(remote, "query { version { version } }")
        v = ver.get("version", {}).get("version", "unknown")
        log.info(f"       OK — Stash v{v}")
    except Exception as exc:
        errors.append(f"Remote unreachable: {exc}")
        log.error(f"       FAIL — {exc}")

    # 2. Destination path writable (local filesystem)
    log.info(f"[2/5] Destination path writable: {dest_path}")
    if os.path.isdir(dest_path):
        test_file = os.path.join(dest_path, ".stash-sync-write-test")
        try:
            with open(test_file, "w") as fh:
                fh.write("ok")
            os.remove(test_file)
            log.info("       OK — directory exists and is writable")
        except OSError as exc:
            errors.append(f"Destination not writable: {exc}")
            log.error(f"       FAIL — not writable: {exc}")
    else:
        try:
            os.makedirs(dest_path, exist_ok=True)
            log.info("       OK — directory created")
        except OSError as exc:
            errors.append(f"Cannot create destination: {exc}")
            log.error(f"       FAIL — cannot create: {exc}")

    # 3. Destination path is inside a remote library path
    log.info(f"[3/5] Destination in remote library: {dest_path}")
    try:
        rcfg = gql(remote, REMOTE_LIBRARY_PATHS)
        stashes = (
            rcfg.get("configuration", {})
            .get("general", {})
            .get("stashes", [])
        )
        lib_paths = [s["path"] for s in stashes]
        matched = any(
            dest_path == lp or dest_path.startswith(lp.rstrip("/") + "/")
            for lp in lib_paths
        )
        if matched:
            log.info(f"       OK — inside remote library")
        else:
            errors.append(
                f"Destination '{dest_path}' is not inside any remote library path. "
                f"Remote libraries: {lib_paths}"
            )
            log.error(
                f"       FAIL — not inside remote library paths: {lib_paths}"
            )
    except Exception as exc:
        errors.append(f"Could not query remote library paths: {exc}")
        log.error(f"       FAIL — {exc}")

    # 4. Transfer tag
    log.info(f"[4/5] Transfer tag: {tag_name}")
    try:
        tag_id = ensure_tag(source, tag_name)
        log.info(f"       OK — tag ID {tag_id}")
    except Exception as exc:
        errors.append(f"Tag issue: {exc}")
        log.error(f"       FAIL — {exc}")

    # 5. Remote scan permission
    log.info("[5/5] Remote scan permission")
    try:
        gql(remote, "query { jobQueue { id } }")
        log.info("       OK — can query remote job queue")
    except Exception as exc:
        errors.append(f"Remote job query failed: {exc}")
        log.error(f"       FAIL — {exc}")

    # Summary
    if errors:
        log.error(f"=== FAILED — {len(errors)} issue(s) found ===")
        for e in errors:
            log.error(f"  • {e}")
    else:
        log.info("=== ALL CHECKS PASSED ===")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    raw = sys.stdin.read()
    json_input = json.loads(raw)

    mode = json_input.get("args", {}).get("mode", "")
    source = StashInterface(json_input["server_connection"])

    # Read plugin settings
    cfg = gql(source, PLUGIN_CONFIG)
    settings = (
        cfg.get("configuration", {})
        .get("plugins", {})
        .get("stash-sync", {})
    )

    remote_url = settings.get("remote_url", "").strip()
    remote_api_key = settings.get("remote_api_key", "").strip()
    remote_name = settings.get("remote_name", "Remote").strip()
    dest_path = settings.get("destination_path", "").strip()
    tag_name = settings.get("transfer_tag", "").strip() or DEFAULT_TRANSFER_TAG

    # Validate
    if not remote_url:
        log.error(
            "Remote URL not configured. "
            "Go to Settings > Plugins > Stash Sync."
        )
        return
    if not dest_path:
        log.error(
            "Destination path not configured. "
            "Go to Settings > Plugins > Stash Sync."
        )
        return

    # Connect to remote instance
    parsed = urlparse(remote_url)
    remote = StashInterface({
        "Scheme": parsed.scheme or "http",
        "Host": parsed.hostname or "localhost",
        "Port": parsed.port or 9999,
        "ApiKey": remote_api_key,
    })

    try:
        ver = gql(remote, "query { version { version } }")
        v = ver.get("version", {}).get("version", "unknown")
        log.info(f"Connected to {remote_name} ({remote_url}) - v{v}")
    except Exception as exc:
        log.error(f"Cannot reach remote instance at {remote_url}: {exc}")
        return

    # --- Test Connection ---
    if mode == "test_connection":
        test_connection(source, remote, remote_name, remote_url, dest_path, tag_name)
        return

    resolver = EntityResolver(source, remote)

    # --- Transfer Single Scene ---
    if mode == "transfer_single":
        scene_id = json_input.get("args", {}).get("scene_id")
        if not scene_id:
            log.error("No scene_id provided")
            return
        try:
            transfer_scene(
                scene_id, source, remote, resolver, dest_path, tag_name, remote_name
            )
        except Exception as exc:
            log.error(f"Transfer failed: {exc}")
        return

    # --- Transfer Tagged Scenes ---
    if mode == "transfer_tagged":
        tag_id = ensure_tag(source, tag_name)
        scenes = find_tagged_scenes(source, tag_id)

        if not scenes:
            log.info("No scenes found with the transfer tag.")
            return

        total = len(scenes)
        log.info(f"Found {total} scene(s) to transfer to {remote_name}")

        ok, failures = 0, []
        for i, scene in enumerate(scenes):
            log.info(f"--- Bulk transfer: scene {i + 1}/{total} (id={scene['id']}) ---")
            try:
                transfer_scene(
                    scene["id"], source, remote, resolver, dest_path, tag_name,
                    remote_name,
                )
                ok += 1
            except Exception as exc:
                t = scene.get("title") or f"Scene {scene['id']}"
                log.error(f"Failed: {t} — {exc}")
                failures.append({"title": t, "id": scene["id"], "error": str(exc)})
            log.progress((i + 1) / total)

        log.info(f"=== Transfer Complete: {ok}/{total} succeeded ===")
        for fail in failures:
            log.warning(f"  FAILED: {fail['title']} (ID {fail['id']}): {fail['error']}")
        return

    # --- Dry Run ---
    if mode == "dry_run":
        tag_id = ensure_tag(source, tag_name)
        dry_run(source, remote, tag_id, tag_name)
        return

    log.error(f"Unknown mode: {mode}")


if __name__ == "__main__":
    main()
