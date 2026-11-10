import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {ci, metric, viewModel} from './web/eci-tuning.mjs';

test('CI formatting leaves missing values unknown', () => {
  assert.equal(ci({eci_posterior_mean:105.3759, eci_p05:94.31056, eci_p95:115.1379}), '105.38 [94.31, 115.14]');
  for (const value of [null, undefined, NaN, Infinity, '1']) assert.equal(metric(value), 'unknown');
});
test('view model preserves separate classes, overlap and pending status', () => {
  const group = {eci:[], notes:['90% CIs overlap; no clean winner.'], components:{}, unknown_latency:['a'], unknown_cost:['a']};
  const view = viewModel({rankings:{baseline_protocol:group, pipeline_configured:group}, pending:[{model:'Pending', status:'NOT_COMPUTED'}], policy:{routes:{general:{model:'fixture', confidence:'low-to-moderate'}}}});
  assert.equal(view.sections.length, 2);
  assert.match(view.sections[0].title, /baseline/);
  assert.match(view.sections[1].title, /separate protocol/);
  assert.match(view.pending[0], /NOT_COMPUTED/);
  assert.match(view.defaultNote, /NOT_APPLIED/);
  assert.match(view.sections[0].notes[0], /no clean winner/);
});
test('page uses local font fallbacks, read-only fetch and safe text rendering', () => {
  const source = readFileSync(new URL('./web/eci-tuning.mjs',import.meta.url),'utf8');
  const css = readFileSync(new URL('./web/eci-tuning.css',import.meta.url),'utf8');
  assert.match(css,/Jost, system-ui/); assert.match(css,/Newsreader, system-ui/);
  assert.doesNotMatch(css,/@import|url\(|shadow|glow/);
  assert.doesNotMatch(source,/innerHTML|method:\s*['"]POST|\/api\/inference\/fix/);
  assert.match(source,/textContent/); assert.match(source,/Status unknown/);
});

test('tuning page exposes guarded repair through an open-only Overview link', () => {
  const html=readFileSync(new URL('./web/eci-tuning.html',import.meta.url),'utf8');
  assert.match(html,/Check and repair inference/);
  assert.match(html,/href="\/#model-tuning"/);
  assert.match(html,/press <strong>Run check<\/strong> to confirm/);
  assert.match(html,/Opening the tool does not start repair/);
  assert.match(html,/Closing the panel does not cancel a running repair/);
  assert.match(html,/Windows remains read-only/);
});
