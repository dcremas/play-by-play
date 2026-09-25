/* Keep every Plotly chart the size of the box it is actually sitting in.
 *
 * Plotly measures its container once, when it draws. `config.responsive` sounds like
 * it fixes that and does not: it re-measures on a **window resize event** and on
 * nothing else. Every way a container changes size in this app is some other way, and
 * each one leaves a chart at a width that was right when it was drawn and is wrong
 * now. Three of them, all measured against the running app rather than guessed at:
 *
 *   reveal        A chart drawn inside an inactive tab panel or the collapsed "Shape
 *                 of the current selection" accordion measures a host of 0, so plotly
 *                 falls back to its default 700. On the player page the two graphs on
 *                 the open tab read 468.5 against a 469px host and the four on the
 *                 closed tabs read 700 against a host of 0 -- so opening Punt used to
 *                 spill the chart 231px past the panel.
 *   re-layout     The explorer's chart row is three columns on one phase and two on
 *                 several, because the third chart only exists for a single phase.
 *                 Going from one phase to three took the hosts 296 -> 452 and left
 *                 both charts drawn at 296.3: 156px of dead space. The other
 *                 direction overflowed by the same amount.
 *   redraw        Setting a new figure does not re-measure either. `Plotly.react`
 *                 keeps the existing size unless the layout says otherwise, which is
 *                 why a new figure arriving in the same callback as the re-layout
 *                 does not rescue it.
 *
 * A resize event fixes all three, and wiring one to each Dash control that can cause
 * them does not: the re-layout case has no control to hang it on, and the next tab
 * set or collapsible added to the app would be broken until someone remembered. So
 * watch the boxes instead of the controls. A ResizeObserver fires on exactly the
 * event we care about -- this element's box changed -- whatever caused it, including
 * the plain window resize plotly already handles.
 *
 * Observing `.js-plotly-plot` rather than the dcc.Graph host is deliberate: plotly
 * sizes that div to 100% of the host, so its box tracks the host's, and it is the
 * element we need a handle on anyway.
 */
(function () {
  "use strict";

  if (typeof ResizeObserver === "undefined") return;

  var observed = new WeakSet();

  var observer = new ResizeObserver(function (entries) {
    for (var i = 0; i < entries.length; i++) {
      var gd = entries[i].target;
      var w = gd.clientWidth;
      var h = gd.clientHeight;
      // Zero width means the panel holding it is still hidden. There is nothing to
      // measure, and the observer will bring it straight back the moment it is shown.
      if (!w || !gd._fullLayout || !window.Plotly) continue;
      // Already right. This is the guard that keeps a resize from feeding itself:
      // every real window resize also arrives here, usually after plotly's own
      // handler has already dealt with it.
      if (Math.abs(gd._fullLayout.width - w) < 1 &&
          Math.abs(gd._fullLayout.height - h) < 1) continue;
      window.Plotly.Plots.resize(gd);
    }
  });

  function attach(el) {
    if (observed.has(el)) return;
    observed.add(el);
    observer.observe(el);
  }

  function scan(root) {
    if (!root || root.nodeType !== 1) return;
    if (root.classList.contains("js-plotly-plot")) attach(root);
    var found = root.querySelectorAll(".js-plotly-plot");
    for (var i = 0; i < found.length; i++) attach(found[i]);
  }

  // Graphs are built by callbacks and replaced whenever a filter changes, so the set
  // has to be picked up as it arrives. Only the added subtrees are scanned, never the
  // whole document: the plays grid mutates constantly while it scrolls, and each of
  // those mutations is a handful of row nodes with no chart in them.
  new MutationObserver(function (records) {
    for (var i = 0; i < records.length; i++) {
      var added = records[i].addedNodes;
      for (var j = 0; j < added.length; j++) scan(added[j]);
    }
  }).observe(document.documentElement, { childList: true, subtree: true });

  scan(document.documentElement);
})();
