(function () {
  "use strict";

  const PLUGIN_ID = "stash-scrape";
  const MENU_ITEM_ID = "stash-scrape-menuitem";
  const TOAST_ID = "stash-scrape-toast";

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

  async function runScrapeTask(sceneId) {
    return callGQL(
      `mutation RunPluginTask($id: ID!, $task: String!, $args: Map) {
         runPluginTask(plugin_id: $id, task_name: $task, args_map: $args)
       }`,
      {
        id: PLUGIN_ID,
        task: "Scrape Single Scene",
        args: { scene_id: String(sceneId) },
      }
    );
  }

  // ---------------------------------------------------------------------------
  // Toast notification
  // ---------------------------------------------------------------------------

  function showNotification(message, isError = false) {
    let toast = document.getElementById(TOAST_ID);
    if (!toast) {
      toast = document.createElement("div");
      toast.id = TOAST_ID;
      toast.className = "stash-scrape-toast";
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.toggle("stash-scrape-toast-error", isError);
    toast.classList.add("stash-scrape-toast-visible");
    clearTimeout(toast._hideTimer);
    toast._hideTimer = setTimeout(() => {
      toast.classList.remove("stash-scrape-toast-visible");
    }, 4000);
  }

  // ---------------------------------------------------------------------------
  // Scene operations dropdown injection
  // ---------------------------------------------------------------------------

  function getSceneIdFromPath() {
    const m = window.location.pathname.match(/\/scene[s]?\/(\d+)/);
    return m ? m[1] : null;
  }

  function removeScrapeMenuItem() {
    const existing = document.getElementById(MENU_ITEM_ID);
    if (existing) existing.remove();
  }

  function isSceneOperationsMenu(menu) {
    const text = menu.textContent || "";
    return text.includes("Rescan") && (text.includes("Delete") || text.includes("Generate"));
  }

  function findSceneOperationsDropdown() {
    for (const menu of document.querySelectorAll(".dropdown-menu.show")) {
      if (isSceneOperationsMenu(menu)) return menu;
    }
    return null;
  }

  async function addScrapeItemToMenu(menu) {
    if (!menu || document.getElementById(MENU_ITEM_ID)) return;
    const sceneId = getSceneIdFromPath();
    if (!sceneId) return;

    const li = document.createElement("li");
    li.id = MENU_ITEM_ID;
    li.dataset.sceneId = sceneId;

    const a = document.createElement("a");
    a.className = "dropdown-item stash-scrape-menuitem";
    a.href = "#";
    a.textContent = "Scrape metadata";

    a.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();

      // Close the dropdown
      document.body.click();

      try {
        await runScrapeTask(sceneId);
        showNotification("Scrape queued — check Tasks for progress");
      } catch (err) {
        showNotification("Failed to start scrape: " + err.message, true);
      }
    });

    li.appendChild(a);

    // Insert before the last divider+delete group so it feels natural
    const dividers = menu.querySelectorAll(".dropdown-divider");
    const lastDivider = dividers[dividers.length - 1];
    if (lastDivider) {
      menu.insertBefore(li, lastDivider);
    } else {
      menu.appendChild(li);
    }
  }

  function tryInjectScrapeMenuItem() {
    if (!getSceneIdFromPath()) {
      removeScrapeMenuItem();
      return;
    }
    const menu = findSceneOperationsDropdown();
    if (menu) addScrapeItemToMenu(menu);
  }

  // ---------------------------------------------------------------------------
  // SPA navigation & DOM mutation wiring
  // ---------------------------------------------------------------------------

  PluginApi.Event.addEventListener("stash:location", () => {
    removeScrapeMenuItem();
    setTimeout(tryInjectScrapeMenuItem, 300);
  });

  setTimeout(tryInjectScrapeMenuItem, 500);

  let injectScheduled = false;
  function scheduleInject() {
    if (!getSceneIdFromPath() || injectScheduled) return;
    injectScheduled = true;
    requestAnimationFrame(() => {
      tryInjectScrapeMenuItem();
      setTimeout(() => { injectScheduled = false; }, 100);
    });
  }

  new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (
        m.type === "childList" ||
        (m.type === "attributes" && m.attributeName === "class")
      ) {
        scheduleInject();
        break;
      }
    }
  }).observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["class"],
  });

  // ---------------------------------------------------------------------------
  // Styles
  // ---------------------------------------------------------------------------

  const style = document.createElement("style");
  style.textContent = `
    .stash-scrape-menuitem {
      cursor: pointer;
    }

    .stash-scrape-toast {
      position: fixed;
      bottom: 1.5rem;
      right: 1.5rem;
      padding: 0.75rem 1.25rem;
      background: var(--bs-success, #198754);
      color: #fff;
      border-radius: 0.25rem;
      box-shadow: 0 0.25rem 0.5rem rgba(0, 0, 0, 0.2);
      z-index: 9999;
      opacity: 0;
      transform: translateY(0.5rem);
      transition: opacity 0.2s, transform 0.2s;
      pointer-events: none;
      max-width: 22rem;
    }
    .stash-scrape-toast.stash-scrape-toast-visible {
      opacity: 1;
      transform: translateY(0);
    }
    .stash-scrape-toast.stash-scrape-toast-error {
      background: var(--bs-danger, #dc3545);
    }
  `;
  document.head.appendChild(style);
})();
