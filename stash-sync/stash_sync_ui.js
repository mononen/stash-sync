(function () {
  "use strict";

  const PLUGIN_ID = "stash-sync";
  const MENU_ITEM_ID = "stash-sync-transfer-menuitem";
  const TOAST_ID = "stash-sync-toast";

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
  // Toast notification (no React / PluginApi.hooks.useToast)
  // ---------------------------------------------------------------------------

  function showNotification(message, isError = false) {
    let toast = document.getElementById(TOAST_ID);
    if (!toast) {
      toast = document.createElement("div");
      toast.id = TOAST_ID;
      toast.className = "stash-sync-toast";
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.toggle("stash-sync-toast-error", isError);
    toast.classList.add("stash-sync-toast-visible");
    clearTimeout(toast._hideTimer);
    toast._hideTimer = setTimeout(() => {
      toast.classList.remove("stash-sync-toast-visible");
    }, 4000);
  }

  // ---------------------------------------------------------------------------
  // Transfer menu item – add under the scene page 3-dot menu
  // ---------------------------------------------------------------------------

  function getSceneIdFromPath() {
    const m = window.location.pathname.match(/\/scene[s]?\/(\d+)/);
    return m ? m[1] : null;
  }

  function removeTransferMenuItem() {
    const existing = document.getElementById(MENU_ITEM_ID);
    if (existing) existing.remove();
  }

  function isSceneOperationsMenu(menu) {
    const text = menu.textContent || "";
    return (
      text.includes("Rescan") &&
      (text.includes("Delete") || text.includes("Generate"))
    );
  }

  function findSceneOperationsDropdown() {
    const menus = document.querySelectorAll(".dropdown-menu.show");
    for (const menu of menus) {
      if (isSceneOperationsMenu(menu)) return menu;
    }
    return null;
  }

  async function addTransferItemToMenu(menu) {
    if (!menu || document.getElementById(MENU_ITEM_ID)) return;
    const sceneId = getSceneIdFromPath();
    if (!sceneId) return;

    const remoteName = await getRemoteName();

    const li = document.createElement("li");
    li.id = MENU_ITEM_ID;
    li.dataset.sceneId = sceneId;
    const a = document.createElement("a");
    a.className = "dropdown-item stash-sync-menuitem";
    a.href = "#";
    a.textContent = "Transfer to " + remoteName;

    a.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (
        !window.confirm(
          "Transfer this scene to " +
            remoteName +
            "?\n\nThe file will be moved and the scene removed from this instance."
        )
      )
        return;

      try {
        await runTransferTask(sceneId);
        showNotification("Transfer started — check Tasks for progress");
      } catch (err) {
        showNotification("Transfer failed to start: " + err.message, true);
      }
    });

    li.appendChild(a);
    menu.appendChild(li);
  }

  function tryInjectTransferMenuItem() {
    if (!getSceneIdFromPath()) {
      removeTransferMenuItem();
      return;
    }
    const menu = findSceneOperationsDropdown();
    if (menu) addTransferItemToMenu(menu);
  }

  PluginApi.Event.addEventListener("stash:location", () => {
    removeTransferMenuItem();
    setTimeout(tryInjectTransferMenuItem, 300);
  });

  setTimeout(tryInjectTransferMenuItem, 500);

  let injectScheduled = false;
  function scheduleInject() {
    if (!getSceneIdFromPath() || injectScheduled) return;
    injectScheduled = true;
    requestAnimationFrame(() => {
      tryInjectTransferMenuItem();
      setTimeout(() => { injectScheduled = false; }, 100);
    });
  }
  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.type === "childList" || (m.type === "attributes" && m.attributeName === "class")) {
        scheduleInject();
        break;
      }
    }
  });
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });

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
    .stash-sync-toast {
      position: fixed;
      bottom: 1.5rem;
      right: 1.5rem;
      padding: 0.75rem 1.25rem;
      background: var(--bs-success, #198754);
      color: #fff;
      border-radius: 0.25rem;
      box-shadow: 0 0.25rem 0.5rem rgba(0,0,0,0.2);
      z-index: 9999;
      opacity: 0;
      transform: translateY(0.5rem);
      transition: opacity 0.2s, transform 0.2s;
      pointer-events: none;
      max-width: 20rem;
    }
    .stash-sync-toast.stash-sync-toast-visible {
      opacity: 1;
      transform: translateY(0);
    }
    .stash-sync-toast.stash-sync-toast-error {
      background: var(--bs-danger, #dc3545);
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
