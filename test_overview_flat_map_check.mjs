// Exercise the real rendered-style audit with bounded DOM/style fixtures.
// No Playwright import, observer process, port or browser is used here.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';

const source = readFileSync(new URL('./checks/overview-flat-map.mjs', import.meta.url), 'utf8');
const start = source.indexOf('const result = await page.evaluate(') + 'const result = await page.evaluate('.length;
const audit = source.slice(start, source.indexOf(', fixture.fleetState.facts);', start));

function fixture() {
  class Element {
    constructor(textContent = '', className = '') {
      Object.assign(this, {textContent, className, tagName:'BUTTON', dataset:{}, children:[], parentElement:null,
        style:{display:'block', visibility:'visible', opacity:'1', color:'rgb(0, 0, 0)', stroke:'none',
          strokeWidth:'0px', webkitTextStrokeWidth:'0px', filter:'none', boxShadow:'none', textShadow:'none',
          backdropFilter:'none', backgroundImage:'none', fontFamily:'"IBM Plex Sans", sans-serif', letterSpacing:'normal'}});
    }
    append(child) {child.parentElement = this; this.children.push(child);}
    getBoundingClientRect() {return {width:100, height:44};}
    getAttribute(name) {return name === 'class' ? this.className : null;}
    querySelectorAll(selector) {
      const all = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
      return selector === '*' ? all : selector === '.fleet-map-node' ? all.filter(child => child.className === 'fleet-map-node')
        : all.filter(child => child.className === 'glow');
    }
  }
  const region = new Element('Show map');
  const map = new Element();
  region.append(map);
  const expected = [{id:'mac', scope:'machine', label:'This Mac'}, {id:'mac:lane', scope:'lane', label:'Local endpoint'}];
  for (const fact of expected) {
    const button = new Element(`${fact.label} · unknown`, 'fleet-map-node');
    button.dataset.node = fact.id;
    map.append(button);
  }
  const heading = new Element('? Answering count unknown; 1 lanes configured');
  const document = {querySelector: selector => ({'#fleetMap':region, '#fleetMapList':map, '#headline':heading})[selector]};
  return {region, map, heading, expected, Element,
    run: () => JSON.parse(JSON.stringify(runInNewContext(`(${audit})(expected)`, {
      document, Element, expected, getComputedStyle:(el, pseudo) => pseudo ? {...el.style, ...el.pseudo} : el.style,
    })))};
}

test('disclosed map audit accepts full machine/lane labels and the new coverage heading', () => {
  const f = fixture();
  assert.deepEqual(f.run(), {hits:[], halos:[], masks:[], invisible:[], headline:{
    text:f.heading.textContent, family:'"IBM Plex Sans", sans-serif', letterSpacing:'normal', crushed:false}});
});

test('missing or truncated full map labels still fail instead of passing an empty audit', () => {
  for (const kind of ['missing', 'truncated']) {
    const f = fixture();
    if (kind === 'missing') f.map.children.pop();
    else f.map.children[1].textContent = 'Local end…';
    assert.deepEqual(f.run().missing, ['mac:lane']);
  }
});

test('the flat audit rejects effects including pseudos, halos, strokes and invisible labels', () => {
  const f = fixture();
  const button = f.map.children[1];
  button.pseudo = {filter:'blur(2px)'};
  button.style.webkitTextStrokeWidth = '2px';
  button.style.color = 'transparent';
  const halo = new f.Element('', 'glow');
  f.map.append(halo);
  const result = f.run();
  assert.ok(result.hits.some(hit => hit.pseudo === '::before' && hit.effects.filter === 'blur(2px)'));
  assert.equal(result.halos.length, 1);
  assert.equal(result.masks.length, 1);
  assert.equal(result.invisible.length, 1);
  button.style.color = 'rgb(0, 0, 0)';
  f.map.style.display = 'none';
  assert.equal(f.run().invisible.length, 2, 'hidden ancestors cannot conceal required labels');
});

test('coverage retains its complete wording, font and tracking contract', () => {
  for (const kind of ['wrong count', 'truncated', 'font', 'tracking', 'hidden']) {
    const f = fixture();
    if (kind === 'wrong count') f.heading.textContent = '■ 1 of 2 lanes answering';
    if (kind === 'truncated') f.heading.textContent = 'Endpoint…';
    if (kind === 'font') f.heading.style.fontFamily = 'Arial';
    if (kind === 'tracking') f.heading.style.letterSpacing = '-1px';
    if (kind === 'hidden') f.heading.style.visibility = 'hidden';
    assert.equal(f.run().headline.crushed, true, kind);
  }
});
