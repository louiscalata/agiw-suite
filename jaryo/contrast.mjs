/** Node-only WCAG sRGB contrast audit of every semantic color pairing used by the kit. */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
export function contrastRatio(a, b) {
  const luminance = hex => {
    if (!/^#[0-9a-f]{6}$/i.test(hex || '')) throw new Error(`Expected opaque six-digit color: ${hex}`);
    const c=hex.slice(1).match(/../g).map(v=>parseInt(v,16)/255).map(v=>v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4);
    return .2126*c[0]+.7152*c[1]+.0722*c[2];
  };
  const values=[luminance(a),luminance(b)].sort((x,y)=>y-x);
  return (values[0]+.05)/(values[1]+.05);
}
export function contrastPairs(tokens) {
  const results=[];
  for (const [theme,p] of Object.entries(tokens.palette)) {
    const pair=(foreground,background,minimum,role)=>results.push({theme,foreground,background,role,minimum,ratio:contrastRatio(p[foreground],p[background])});
    for (const bg of ['bg','surface']) {
      for (const fg of ['text','muted','status_ok','status_run','status_unknown']) pair(fg,bg,4.5,'body');
      for (const fg of ['border','focus_ring']) pair(fg,bg,3,'UI');
    }
    pair('status_blocked','status_blocked_bg',4.5,'blocked body and hatch');
    for (const fg of ['selected_fg','sel_sub','sel_blocked','sel_run']) pair(fg,'selected_bg',4.5,'selected body');
    pair('accent_ink','accent_identity',4.5,'brand and primary label');
    // The red fill has an explicit page-ink boundary; red alone is not a UI boundary.
    pair('text','accent_identity',3,'primary boundary (inner edge)');
    pair('focus_ring','status_blocked_bg',3,'blocked outer focus');
  }
  return results;
}
export function checkContrast(tokens) {
  return contrastPairs(tokens).filter(p=>p.ratio<p.minimum).map(p=>`${p.theme} ${p.foreground}/${p.background}: ${p.ratio.toFixed(3)}:1 < ${p.minimum}:1 (${p.role})`);
}
if (process.argv[1] && fileURLToPath(import.meta.url)===process.argv[1]) {
  const tokens=JSON.parse(readFileSync(new URL('./tokens.json',import.meta.url),'utf8'));
  const pairs=contrastPairs(tokens), failures=checkContrast(tokens);
  for (const p of pairs) console.log(`${p.ratio>=p.minimum?'PASS':'FAIL'} ${p.theme} ${p.foreground}/${p.background} ${p.ratio.toFixed(3)}:1 >= ${p.minimum}:1`);
  console.log(`${pairs.length} pairs; ${failures.length} failures`);
  process.exitCode=failures.length?1:0;
}
