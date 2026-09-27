(function () {
  "use strict";
  // conduit_push has no dashboard UI; it only mounts backend routes for the
  // Conduit iOS app. Register an empty component so the dashboard doesn't
  // report the plugin as failing to load.
  if (!window.__HERMES_PLUGINS__) return;
  window.__HERMES_PLUGINS__.register("conduit_push", function () {
    return null;
  });
})();
