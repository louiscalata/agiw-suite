import { readFileSync } from 'node:fs';
import { contrastPairs } from './contrast.mjs';

const tokens = JSON.parse(readFileSync(new URL('./tokens.json', import.meta.url), 'utf8'));
const pairs = contrastPairs(tokens);
let failures = 0;
for (const { theme, foreground, background, ratio, minimum } of pairs) {
  const failed = ratio < minimum;
  if (failed) failures += 1;
  console.log(`${failed ? 'FAIL' : 'PASS'} ${theme} ${foreground}/${background} ${ratio.toFixed(3)}:1 >= ${minimum}:1`);
}
console.log(`${pairs.length} pairs; ${failures} failures`);
process.exitCode = failures ? 1 : 0;
