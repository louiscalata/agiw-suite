// Unknown color syntax must fail the ink check rather than look achromatic.
export function chromaOf(color) {
  const value = String(color ?? '').trim().toLowerCase();
  if (!value || value === 'none' || value === 'transparent') return 0;
  const match = value.match(/^rgba?\(([^()]*)\)$/);
  if (!match) return Infinity;

  const commaSyntax = match[1].includes(',');
  const parts = match[1].split(commaSyntax ? /\s*,\s*/ : /\s*\/\s*/);
  const channels = (commaSyntax ? parts.slice(0, 3) : parts[0].trim().split(/\s+/)).map(channel => channel.trim());
  const alpha = commaSyntax ? parts[3] : parts[1];
  const number = /^[-+]?(?:\d+(?:\.\d*)?|\.\d+)%?$/;
  if (channels.length !== 3 || parts.length > (commaSyntax ? 4 : 2)
      || !channels.every(channel => number.test(channel))
      || (alpha !== undefined && !number.test(alpha.trim()))) return Infinity;
  if (alpha !== undefined && parseFloat(alpha) === 0) return 0;
  const rgb = channels.map(channel => parseFloat(channel) * (channel.endsWith('%') ? 2.55 : 1));
  if (!rgb.every(Number.isFinite)) return Infinity;
  return Math.max(...rgb) - Math.min(...rgb);
}
