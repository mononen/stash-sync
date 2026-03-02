(function () {
  "use strict";

  const PLUGIN_ID = "stash-sync";
  const BTN_ID = "stash-sync-transfer-btn";

  // ---------------------------------------------------------------------------
  // GraphQL helpers
  // ---------------------------------------------------------------------------

  async function callGQL(query, variables) {
    const resp = await fetch("/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, variables }),
    });
    const json = await resp.json();
    if (json.errors) throw new Error(json.errors[0].message);
    return json.data;
  }

  let _remoteName = null;
  async function getRemoteName() {
    if (_remoteName) return _remoteName;
    try {
      const data = await callGQL(
        "query { configuration { plugins } }"
      );
      const cfg = (data.configuration?.plugins ?? {})[PLUGIN_ID] ?? {};
      _remoteName = cfg.remote_name || "Remote";
    } catch {
      _remoteName = "Remote";
    }
    return _remoteName;
  }

  async function runTransferTask(sceneId) {
    return callGQL(
      `mutation RunPluginTask($id: ID!, $task: String!, $args: Map) {
         runPluginTask(plugin_id: $id, task_name: $task, args_map: $args)
       }`,
      {
        id: PLUGIN_ID,
        task: "Transfer Single Scene",
        args: { scene_id: String(sceneId) },
      }
    );
  }

  // ---------------------------------------------------------------------------
  // Transfer button – injected into the DOM (no React patching)
  // ---------------------------------------------------------------------------

  function getSceneIdFromPath() {
    const m = window.location.pathname.match(/\/scenes\/(\d+)/);
    return m ? m[1] : null;
  }

  function removeTransferButton() {
    const existing = document.getElementById(BTN_ID);
    if (existing) existing.remove();
  }

  async function injectTransferButton() {
    const sceneId = getSceneIdFromPath();
    if (!sceneId) {
      removeTransferButton();
      return;
    }

    // Already injected for this scene
    const existing = document.getElementById(BTN_ID);
    if (existing && existing.dataset.sceneId === sceneId) return;
    removeTransferButton();

    // Find the scene page toolbar / header to attach to
    const toolbar =
      document.querySelector(".scene-toolbar") ||
      document.querySelector(".scene-header") ||
      document.querySelector("#scene-page-container .scene-info") ||
      document.querySelector("#scene-page-container");

    if (!toolbar) return;

    const remoteName = await getRemoteName();

    const btn = document.createElement("button");
    btn.id = BTN_ID;
    btn.dataset.sceneId = sceneId;
    btn.className = "btn btn-primary stash-sync-btn";
    btn.textContent = "Transfer to " + remoteName;

    btn.addEventListener("click", async () => {
      if (
        !window.confirm(
          "Transfer this scene to " +
            remoteName +
            "?\n\nThe file will be moved and the scene removed from this instance."
        )
      )
        return;

      btn.disabled = true;
      btn.textContent = "Starting transfer\u2026";

      try {
        await runTransferTask(sceneId);
        btn.className = "btn btn-success stash-sync-btn";
        btn.textContent = "Transfer started \u2014 check Tasks for progress";
      } catch (err) {
        btn.disabled = false;
        btn.textContent = "Transfer to " + remoteName;
        window.alert("Transfer failed to start: " + err.message);
      }
    });

    toolbar.appendChild(btn);
  }

  // Re-inject on navigation
  PluginApi.Event.addEventListener("stash:location", () => {
    setTimeout(injectTransferButton, 300);
  });

  // Also try on initial load
  setTimeout(injectTransferButton, 500);

  // ---------------------------------------------------------------------------
  // Mask the API key field in plugin settings (DOM-only, settings page)
  // ---------------------------------------------------------------------------

  function maskApiKeyFields() {
    // Only act on the settings page
    if (!window.location.pathname.includes("/settings")) return;

    for (const h of document.querySelectorAll("h6, h5, label")) {
      if (h.textContent.trim() !== "Remote API Key") continue;

      let container = h.parentElement;
      while (
        container &&
        container.children.length < 2 &&
        container.parentElement !== document.body
      ) {
        container = container.parentElement;
      }
      if (!container || container.dataset.ssMasked) continue;
      container.dataset.ssMasked = "true";

      Array.from(container.children).forEach((child) => {
        if (child.contains(h)) return;
        if (child.matches("button, .btn, [class*='btn']")) return;
        child.classList.add("ss-blurred-value");

        child.querySelectorAll("input[type='text']").forEach((inp) => {
          inp.type = "password";
          inp.autocomplete = "off";
        });
      });
    }

    // Catch late-rendered inputs (after clicking Edit)
    document
      .querySelectorAll(".ss-blurred-value input[type='text']")
      .forEach((inp) => {
        inp.type = "password";
        inp.autocomplete = "off";
      });
  }

  new MutationObserver(maskApiKeyFields).observe(document.body, {
    childList: true,
    subtree: true,
  });

  // ---------------------------------------------------------------------------
  // Styles
  // ---------------------------------------------------------------------------

  const style = document.createElement("style");
  style.textContent = `
    .stash-sync-btn {
      margin: 0.5rem;
    }
    .ss-blurred-value {
      filter: blur(5px);
      transition: filter 0.15s;
      cursor: pointer;
    }
    .ss-blurred-value:hover {
      filter: none;
    }
  `;
  document.head.appendChild(style);
})();
