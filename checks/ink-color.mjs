// Chroma for the overview ink check. RGB uses the channel span (max - min).
// OKLab and OKLCH use chroma = sqrt(a^2 + b^2), then scale it onto that same
// 0-255 span. CSS treats 0.4 as 100% chroma. Unknown syntax returns Infinity
// so a new color function fails closed instead of looking gray.
// Chromium 131 serializes some SVG strokes as oklab(). A near-gray stroke
// such as oklab(0.961073 0.0000438988 0.0000191927) must stay under the
// ink threshold.

const OKLAB_CHROMA_REF = 0.4;
const NUMBER = /^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?%?$/i;

export function chromaOf(color) {
  const value = String(color ?? '').trim().toLowerCase();
  if (!value || value === 'none' || value === 'transparent') return 0;
  const rgb = value.match(/^rgba?\(([^()]*)\)$/);
  if (rgb) return rgbChroma(rgb[1]);
  const lab = value.match(/^oklab\(([^()]*)\)$/);
  if (lab) return oklabChroma(lab[1]);
  const lch = value.match(/^oklch\(([^()]*)\)$/);
  if (lch) return oklchChroma(lch[1]);
  return Infinity;
}

function rgbChroma(body) {
  const commaSyntax = body.includes(',');
  const parts = body.split(commaSyntax ? /\s*,\s*/ : /\s*\/\s*/);
  const channels = (commaSyntax ? parts.slice(0, 3) : parts[0].trim().split(/\s+/)).map(channel => channel.trim());
  const alpha = commaSyntax ? parts[3] : parts[1];
  if (channels.length !== 3 || parts.length > (commaSyntax ? 4 : 2)
      || !channels.every(channel => NUMBER.test(channel))
      || (alpha !== undefined && !NUMBER.test(alpha.trim()))) return Infinity;
  if (alpha !== undefined && parseFloat(alpha) === 0) return 0;
  const rgb = channels.map(channel => parseFloat(channel) * (channel.endsWith('%') ? 2.55 : 1));
  if (!rgb.every(Number.isFinite)) return Infinity;
  return Math.max(...rgb) - Math.min(...rgb);
}

function splitModern(body) {
  if (body.includes(',')) return null;
  const parts = body.trim().split(/\s*\/\s*/);
  if (parts.length > 2 || !parts[0].trim()) return null;
  return { channels: parts[0].trim().split(/\s+/), alpha: parts[1] };
}

function alphaOf(token) {
  if (token === undefined) return 1;
  const value = token.trim();
  if (!NUMBER.test(value)) return null;
  const number = parseFloat(value) * (value.endsWith('%') ? 0.01 : 1);
  return Number.isFinite(number) ? number : null;
}

function component(token, { light = false } = {}) {
  const value = token.trim();
  if (value === 'none') return 0;
  if (!NUMBER.test(value)) return null;
  const number = parseFloat(value);
  if (!Number.isFinite(number)) return null;
  if (!value.endsWith('%')) return number;
  return light ? number / 100 : number / 100 * OKLAB_CHROMA_REF;
}

function hueDegrees(token) {
  const value = token.trim();
  if (value === 'none') return 0;
  const match = value.match(/^([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?)(deg|grad|rad|turn)?$/i);
  if (!match) return null;
  const number = parseFloat(match[1]);
  if (!Number.isFinite(number)) return null;
  switch ((match[2] || 'deg').toLowerCase()) {
    case 'deg': return number;
    case 'turn': return number * 360;
    case 'rad': return number * 180 / Math.PI;
    case 'grad': return number * 0.9;
    default: return null;
  }
}

function scaledChroma(a, b, alpha) {
  if (alpha === 0) return 0;
  const chroma = Math.hypot(a, b);
  if (!Number.isFinite(chroma)) return Infinity;
  return chroma / OKLAB_CHROMA_REF * 255;
}

function oklabChroma(body) {
  const parsed = splitModern(body);
  if (!parsed || parsed.channels.length !== 3) return Infinity;
  const alpha = alphaOf(parsed.alpha);
  if (alpha === null) return Infinity;
  const lightness = component(parsed.channels[0], { light: true });
  const a = component(parsed.channels[1]);
  const b = component(parsed.channels[2]);
  if (lightness === null || a === null || b === null) return Infinity;
  return scaledChroma(a, b, alpha);
}

function oklchChroma(body) {
  const parsed = splitModern(body);
  if (!parsed || parsed.channels.length !== 3) return Infinity;
  const alpha = alphaOf(parsed.alpha);
  if (alpha === null) return Infinity;
  const lightness = component(parsed.channels[0], { light: true });
  const chroma = component(parsed.channels[1]);
  const hue = hueDegrees(parsed.channels[2]);
  if (lightness === null || chroma === null || hue === null) return Infinity;
  const radians = hue * Math.PI / 180;
  return scaledChroma(chroma * Math.cos(radians), chroma * Math.sin(radians), alpha);
}

// Only the opaque booklet accent is admitted, and only at display roles by the
// caller. Unknown serializations keep failing closed through chromaOf.
export function isWebAccent(color) {
  const match = String(color ?? '').trim().toLowerCase().match(/^rgba?\(([^()]*)\)$/);
  if (!match) return false;
  const body = match[1];
  const comma = body.includes(',');
  const parts = body.split(comma ? /\s*,\s*/ : /\s*\/\s*/);
  const channels = (comma ? parts.slice(0, 3) : parts[0].trim().split(/\s+/)).map(value => value.trim());
  if (channels.length !== 3 || parts.length > (comma ? 4 : 2)
      || !channels.every(value => NUMBER.test(value))) return false;
  if (alphaOf(comma ? parts[3] : parts[1]) !== 1) return false;
  const values = channels.map(value => parseFloat(value) * (value.endsWith('%') ? 2.55 : 1));
  return values.every((value, index) => Math.abs(value - [194, 26, 39][index]) < 0.001);
}
