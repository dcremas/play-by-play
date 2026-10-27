/* Load plotly.js's basic bundle instead of the full one.
 *
 * dcc.Graph lazy-loads plotly.js on first render from `window._dashPlotlyJSURL`, which
 * the Dash renderer points at the full bundle shipped inside the plotly package: 4.2 MB
 * (1.3 MB gzipped) and about 1.7 s of parse and run on a mid-range phone, the largest
 * single file on the page (Lighthouse, 2026-10-27). Every chart here is go.Scatter or
 * go.Bar, and the basic bundle (scatter, bar, pie) draws both for 1.2 MB (395 KB
 * gzipped).
 *
 * The renderer sets the URL in an effect after this script has run, so a plain
 * assignment here would be overwritten. A property whose setter ignores writes keeps
 * ours. Loading stays lazy: nothing is fetched until a chart mounts.
 *
 * The bundle lives in assets/vendor/, which app.py keeps out of Dash's automatic
 * <script> tags (assets_path_ignore); otherwise it would load eagerly on every page. Its
 * version must match the plotly.js inside the installed plotly package:
 *   .venv/bin/python -c "from plotly.offline import get_plotlyjs_version as v; print(v())"
 * A chart type outside scatter/bar/pie (heatmap, histogram, box...) needs the full
 * bundle back: delete this file.
 */
(function () {
  var src = document.currentScript && document.currentScript.src;
  if (!src) return;
  var url = src.replace(/plotly-basic\.js(\?.*)?$/, "vendor/plotly-basic-4.0.0.min.js");
  Object.defineProperty(window, "_dashPlotlyJSURL", {
    configurable: true,
    get: function () { return url; },
    set: function () {},
  });
})();
