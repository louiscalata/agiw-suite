// Runs inside the browser. Bounds come from the rendered document, not CSS rules.
export function overviewGeometry() {
  const failures = [];
  const width = document.documentElement.clientWidth;
  const tolerance = 1; // Fractional layout/scrollWidth rounding, not a layout allowance.
  const outside = (rect, bounds, axis) => axis === 'x'
    ? rect.left < bounds.left - tolerance || rect.right > bounds.right + tolerance
    : rect.top < bounds.top - tolerance || rect.bottom > bounds.bottom + tolerance;
  if (Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) > width + tolerance) {
    failures.push({ target: 'document', reason: 'horizontal overflow' });
  }
  const inspect = (element, target, inViewport = false) => {
    if (!element) {
      failures.push({ target, reason: 'missing' });
      return;
    }
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0 || !element.textContent.trim()) {
      failures.push({ target, reason: 'empty or zero area' });
      return;
    }
    const range = document.createRange();
    range.selectNodeContents(element);
    const text = range.getBoundingClientRect();
    if (outside(rect, { left: 0, right: width }, 'x') || outside(text, { left: 0, right: width }, 'x')) {
      failures.push({ target, reason: 'outside horizontal viewport' });
    }
    if (inViewport && outside(rect, { top: 0, bottom: window.innerHeight }, 'y')) {
      failures.push({ target, reason: 'outside vertical viewport' });
    }
    for (let parent = element; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' || Number(style.opacity) === 0) {
        failures.push({ target, reason: 'hidden' });
        break;
      }
      const bounds = parent.getBoundingClientRect();
      for (const [axis, overflow] of [['x', style.overflowX], ['y', style.overflowY]]) {
        // Document scrolling is allowed; clipped text inside a control or tab is not.
        if (axis === 'y' && (parent === document.body || parent === document.documentElement)
            && ['auto', 'scroll', 'visible'].includes(overflow)) continue;
        if (['hidden', 'clip', 'auto', 'scroll'].includes(overflow) && outside(text, bounds, axis)) {
          failures.push({ target, reason: `text clipped on ${axis}`,
            ancestor: String(parent.id || parent.className || parent.tagName || ''),
            textBounds: { left: text.left, right: text.right, top: text.top, bottom: text.bottom },
            clipBounds: { left: bounds.left, right: bounds.right, top: bounds.top, bottom: bounds.bottom } });
        }
      }
    }
  };
  inspect(document.querySelector('.brand-wordmark'), 'heading', true);
  for (const [selector, label] of [['.brand-wordmark strong', 'AGIW'], ['.brand-wordmark small', 'Inference Monitor']]) {
    const part = document.querySelector(selector);
    inspect(part, label, true);
    if (part && part.textContent.trim() !== label) failures.push({ target: label, reason: 'full label changed' });
  }
  const tuning = document.querySelector('#fixInference .fix-tab-long');
  inspect(tuning, 'Model tuning', true);
  if (tuning && tuning.textContent.trim() !== 'Model tuning') {
    failures.push({ target: 'Model tuning', reason: 'full label changed' });
  }
  const tabs = [...document.querySelectorAll('#monitorTabs .j-tab-label')];
  if (tabs.length < 2) failures.push({ target: 'status tabs', reason: 'demo fixture did not populate multiple tabs' });
  tabs.forEach((tab, index) => inspect(tab, `status tab ${index + 1}`));
  return failures;
}


// Failure-only state, excluding snapshots, label text and response bodies.
export function demoReadinessState() {
  const host = document.querySelector('#monitorTabs');
  const labels = [...document.querySelectorAll('#monitorTabs .j-tab-label')];
  const second = labels[1];
  const ancestors = [];
  for (let node = second; node && ancestors.length < 8; node = node.parentElement) {
    const style = getComputedStyle(node);
    const box = node.getBoundingClientRect();
    ancestors.push({ tag: node.tagName, id: node.id || null, hidden: node.hidden,
      display: style.display, visibility: style.visibility, opacity: style.opacity,
      overflowX: style.overflowX, overflowY: style.overflowY,
      bounds: { x: box.x, y: box.y, width: box.width, height: box.height } });
  }
  return { readyState: document.readyState, labelCount: labels.length,
    tabCount: host?.querySelectorAll('[role="tab"]').length ?? 0,
    bannerHidden: document.querySelector('#jaryoDemo')?.hidden ?? null,
    fictional: host?.dataset.fictional ?? null, secondExists: Boolean(second), ancestors };
}
