(function () {
  "use strict";

  const PLUGIN_ID = "stash-sync";

  // ---------------------------------------------------------------------------
  // GraphQL helpers (use the page's own /graphql endpoint)
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

  async function getRemoteName() {
    try {
      const data = await callGQL(
        "query { configuration { plugins } }"
      );
      const cfg = (data.configuration?.plugins ?? {})[PLUGIN_ID] ?? {};
      return cfg.remote_name || "Remote";
    } catch {
      return "Remote";
    }
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
  // React component – transfer button
  // ---------------------------------------------------------------------------

  function TransferButton() {
    const React = PluginApi.React;
    const [remoteName, setRemoteName] = React.useState(null);
    const [busy, setBusy] = React.useState(false);
    const [done, setDone] = React.useState(false);

    React.useEffect(() => {
      getRemoteName().then(setRemoteName);
    }, []);

    const sceneId = getSceneIdFromPath();
    if (!sceneId || remoteName === null) return null;

    async function handleClick() {
      if (
        !window.confirm(
          `Transfer this scene to ${remoteName}?\n\nThe file will be moved and the scene will be removed from this instance.`
        )
      )
        return;

      setBusy(true);
      try {
        await runTransferTask(sceneId);
        setDone(true);
      } catch (err) {
        window.alert("Transfer failed to start: " + err.message);
      } finally {
        setBusy(false);
      }
    }

    if (done) {
      return React.createElement(
        "div",
        { className: "stash-sync-btn-wrapper" },
        React.createElement(
          "button",
          { className: "btn btn-success", disabled: true },
          "Transfer started — check Tasks for progress"
        )
      );
    }

    return React.createElement(
      "div",
      { className: "stash-sync-btn-wrapper" },
      React.createElement(
        "button",
        {
          className: "btn btn-primary",
          disabled: busy,
          onClick: handleClick,
        },
        busy ? "Starting transfer…" : `Transfer to ${remoteName}`
      )
    );
  }

  // ---------------------------------------------------------------------------
  // Patch ScenePage to append the button
  // ---------------------------------------------------------------------------

  PluginApi.patch.after("ScenePage", function (_props, result) {
    const React = PluginApi.React;
    return React.createElement(
      React.Fragment,
      null,
      result,
      React.createElement(TransferButton)
    );
  });

  // ---------------------------------------------------------------------------
  // Utility
  // ---------------------------------------------------------------------------

  function getSceneIdFromPath() {
    const m = window.location.pathname.match(/\/scenes\/(\d+)/);
    return m ? m[1] : null;
  }

  // ---------------------------------------------------------------------------
  // Mask the API key field in plugin settings
  // ---------------------------------------------------------------------------

  function maskApiKeyInputs() {
    document.querySelectorAll(".setting").forEach((setting) => {
      const heading = setting.querySelector("h6, label, .setting-header");
      if (!heading) return;
      if (!heading.textContent.includes("Remote API Key")) return;
      const input = setting.querySelector('input[type="text"]');
      if (input) {
        input.type = "password";
        input.autocomplete = "off";
      }
    });
  }

  new MutationObserver(maskApiKeyInputs).observe(document.body, {
    childList: true,
    subtree: true,
  });

  // Minimal styling injected once
  const style = document.createElement("style");
  style.textContent = `
    .stash-sync-btn-wrapper {
      padding: 0.5rem 1rem;
      display: flex;
      justify-content: flex-end;
    }
  `;
  document.head.appendChild(style);
})();
