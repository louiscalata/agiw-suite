import test from 'node:test';
import { runInNewContext } from 'node:vm';
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync, cpSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { checkKit, checkPlatformVariants, checkChoiceCss } from './jaryo/contract.mjs';
import { contrastPairs, checkContrast, contrastRatio } from './jaryo/contrast.mjs';
import { declaredNeighbours, eventTick, hiddenGroup, mountChoicePipeline, mountCommandPalette } from './jaryo/interactions.mjs';
import { mount as mountKit } from './jaryo/behavior.mjs';
import * as mapTabs from './web/monitor-tabs.mjs';
import { CHOSEN, CHOICE_FILE, OVERRIDES } from './jaryo/decision.mjs';
const tokens=JSON.parse(readFileSync('jaryo/tokens.json','utf8'));

test('all 17 source choices and the separate dated F17 override are exact',()=>{
  const bytes=readFileSync('jaryo/choices.json');
  assert.equal(createHash('sha256').update(bytes).digest('hex'),CHOICE_FILE.sha256);
  assert.deepEqual(JSON.parse(bytes).choices,CHOSEN);
  assert.deepEqual(tokens.chosenFamilyIds,CHOSEN);
  assert.equal(Object.keys(CHOSEN).length,17);
  assert.deepEqual(tokens.ownerOverrides,OVERRIDES);
  assert.equal(tokens.chosenFamilyIds.F17,'F17.01');
  assert.equal(tokens.ownerOverrides.F17.date,'2026-11-11');
  assert.equal(tokens.ownerOverrides.F17.reason,'overridden by owner: booklet red');
});
for (const [name,mutate] of [
  ['choice ID',t=>t.chosenFamilyIds.F08='F08.01'],
  ['override date',t=>t.ownerOverrides.F17.date='2026-11-10'],
  ['copper accent',t=>t.palette.dark.accent_identity='#C47A3A'],
  ['surface',t=>t.palette.dark.surface='#181E20'],
  ['thumb bar',t=>t.interaction.thumbBar='40px'],
  ['pattern',t=>t.patterns.inputs='ordinary-field'],
]) test(`contract rejects drift in ${name}`,()=>{ const changed=structuredClone(tokens); mutate(changed); assert.ok(checkPlatformVariants(changed).length); });

test('all semantic text and UI pairs meet their AA minimum',()=>{
  assert.equal(contrastPairs(tokens).length,44);
  assert.deepEqual(checkContrast(tokens),[]);
  assert.equal(contrastRatio('#000000','#FFFFFF'),21);
  assert.throws(()=>contrastRatio('transparent','#FFFFFF'),/opaque/);
});
test('contrast audit fails real color regressions and documents the prohibited red text pair',()=>{
  const changed=structuredClone(tokens); changed.palette.dark.muted=changed.palette.dark.surface;
  assert.ok(checkContrast(changed).some(message=>message.includes('muted/surface')));
  assert.ok(contrastRatio(tokens.palette.dark.accent_identity,tokens.palette.dark.surface)<3);
  assert.ok(contrastRatio(tokens.palette.dark.accent_ink,tokens.palette.dark.accent_identity)>4.5);
});
for (const [file,from,to] of [
  ['jaryo/primitives.css','--j-surface: #141A1C','--j-surface: #222222'],
  ['web/jaryo.css','--j-accent: #C21A27','--j-accent: #C47A3A'],
  ['web/jaryo.css','height: calc(var(--j-thumb-bar)','height: calc(40px'],
  ['web/jaryo.mjs','Dependencies not declared','Dependencies guessed'],
]) test(`file contract catches ${file} export drift (${from})`,()=>{
  const root=mkdtempSync(join(tmpdir(),'jaryo-contract-'));
  try {
    cpSync('jaryo',join(root,'jaryo'),{recursive:true});
    // Only the two UI exports are needed alongside the copied standalone kit.
    cpSync('web',join(root,'web'),{recursive:true});
    const original=readFileSync(join(root,file),'utf8'); assert.ok(original.includes(from));
    writeFileSync(join(root,file),original.replace(from,to));
    assert.ok(checkKit(join(root,'jaryo')).some(problem=>/generated|token values/.test(problem)));
  } finally {rmSync(root,{recursive:true,force:true});}
});

test('dependency order is declared, branches stay plural and array order never invents neighbours',()=>{
  const steps=[{id:'a',inputs:[]},{id:'b',inputs:['a']},{id:'c',inputs:['a']},{id:'d'}];
  assert.deepEqual(declaredNeighbours(steps,'a').outputs.map(s=>s.id),['b','c']);
  assert.equal(declaredNeighbours(steps,'d').declared,false);
  assert.deepEqual(declaredNeighbours(steps,'d').inputs,[]);
});
test('hidden groups cannot conceal attention, and collapsed required steps say they will run',()=>{
  for (const word of ['BLOCKED','UNKNOWN','REVIEW','STALE']) assert.equal(hiddenGroup([{word}]).locked,true);
  assert.equal(hiddenGroup([{word:'OK'}]).label,'[+1 optional] steps — off by default');
  assert.equal(hiddenGroup([{word:'OK'}],'collapsed').label,'[+1 collapsed] steps — will run');
});
test('event ticks deduplicate receipts, cap glyphs at eight and reset per run',()=>{
  let state=null;
  assert.equal(eventTick(state,{}),null);
  for(let i=0;i<13;i++) state=eventTick(state,{runId:'one',id:String(i)});
  assert.equal(state.count,13); assert.equal(state.glyphs.length,8);
  assert.equal(eventTick(state,{runId:'one',id:'12'}),state);
  const next=eventTick(state,{runId:'two',id:'12'}); assert.equal(next.count,1); assert.equal(next.label,'1 this run');
});

// Small DOM test double: exercises actual event handlers without a browser or sockets.
class Element {
  constructor(doc,tag) { Object.assign(this,{ownerDocument:doc,tagName:tag.toUpperCase(),children:[],listeners:{},attrs:{},dataset:{},className:'',hidden:false,disabled:false,open:false,value:'',text:''}); }
  get classList(){return {add:(...names)=>{this.className+=' '+names.join(' ');}};}
  get isConnected(){return this===this.ownerDocument.body || Boolean(this.parentElement?.isConnected);}
  get textContent(){return this.text+this.children.map(c=>c.textContent).join('');}
  set textContent(value){this.text=String(value);this.replaceChildren();}
  get lastChild(){return this.children.at(-1);}
  contains(node){return this===node || this.children.some(child=>child.contains(node));}
  append(...nodes){for(const node of nodes){node.remove();node.parentElement=this;this.children.push(node);}}
  insertBefore(node,ref){node.remove();node.parentElement=this;this.children.splice(this.children.indexOf(ref),0,node);}
  replaceChildren(...nodes){for(const child of [...this.children])child.remove();this.append(...nodes);}
  remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(c=>c!==this);this.parentElement=null;}
  setAttribute(k,v){this.attrs[k]=String(v);if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(v);}
  getAttribute(k){return this.attrs[k]??null;}
  removeAttribute(k){delete this.attrs[k];}
  removeEventListener(k,fn){this.listeners[k]=(this.listeners[k]||[]).filter(item=>item!==fn);}
  addEventListener(k,fn){(this.listeners[k]||=[]).push(fn);}
  dispatch(k,extra={}){for(const fn of this.listeners[k]||[])fn({key:'',preventDefault(){},stopPropagation(){},...extra});}
  click(){if(!this.disabled)this.dispatch('click');}
  focus(){this.ownerDocument.activeElement=this;}
  show(){this.open=true;this.modal=false;}
  showModal(){if(!this.isConnected)throw new DOMException('Dialog is disconnected','InvalidStateError');this.open=true;this.modal=true;}
  close(){if(this.open){this.open=false;this.modal=false;if(this.ownerDocument.deferClose)this.ownerDocument.closeEvents.push(()=>this.dispatch('close'));else this.dispatch('close');}}
  matches(selector){
    if(selector.includes(':modal'))return this.open&&this.modal&&this.matches(selector.replace(':modal','')||'*');
    if(selector.startsWith('#'))return this.id===selector.slice(1);
    const cls=selector.match(/^\.([\w-]+)/)?.[1];
    if(cls&&!this.className.split(/\s+/).includes(cls))return false;
    const tag=selector.match(/^[a-z]+/)?.[0];if(tag&&this.tagName!==tag.toUpperCase())return false;
    for(const [,key,value] of selector.matchAll(/\[([^=\]]+)(?:="([^"]*)")?\]/g)) {
      const actual=key==='open'?(this.open?'':null):key.startsWith('data-')?this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]:this.getAttribute(key);
      if(value===undefined?actual==null:actual!==value)return false;
    }
    return true;
  }
  closest(selector){for(let node=this;node;node=node.parentElement)if(node.matches(selector))return node;return null;}
  querySelectorAll(selector){const out=[];for(const child of this.children){if(child.matches(selector))out.push(child);out.push(...child.querySelectorAll(selector));}return out;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}
function dom(phone=false){
  const listeners=[];const media={matches:phone,addEventListener:(_,fn)=>listeners.push(fn),removeEventListener:(_,fn)=>listeners.splice(listeners.indexOf(fn),1)};
  const doc={closeEvents:[],flushClose(){for(const fn of this.closeEvents.splice(0))fn();},defaultView:{matchMedia:()=>media},createElement:tag=>new Element(doc,tag)};
  doc.body=new Element(doc,'body');doc.documentElement={dataset:{}};const events=new Element(doc,'document');doc.addEventListener=events.addEventListener.bind(events);doc.dispatch=events.dispatch.bind(events);doc.dispatchEvent=e=>doc.dispatch(e.type,e);doc.querySelector=s=>doc.body.querySelector(s);doc.querySelectorAll=s=>doc.body.querySelectorAll(s);
  const host=doc.createElement('div');doc.body.append(host);
  return {doc,host,resize:phone=>{media.matches=phone;listeners.forEach(fn=>fn());}};
}
const sample=[{id:'a',name:'Source',word:'OK',inputs:[]},{id:'b',name:'Branch B',word:'UNKNOWN',inputs:['a']},{id:'c',name:'Branch C',word:'BLOCKED',inputs:['a']}];
test('mounted spine exposes IDs and real neighbours, with fork chooser rather than guessed Next',()=>{
  const {doc,host}=dom();mountChoicePipeline(host,{steps:sample,selectedId:'a'});
  assert.match(host.querySelector('.jp-step').getAttribute('aria-label'),/step_id: a/);
  assert.equal(host.querySelectorAll('.jp-step')[1].dataset.neighbour,'output →');
  const next=host.querySelector('[data-direction="Next"]');assert.equal(next.textContent,'Next choose (2)');next.click();
  const branches=doc.querySelector('.jp-branches');assert.equal(branches.open,true);
  assert.equal(branches.querySelectorAll('button').length,3);
  branches.querySelector('button').click();assert.equal(host.querySelectorAll('.jp-step')[1].getAttribute('aria-pressed'),'true');
  assert.equal(host.querySelector('.jp-inspector').open,true);
});
test('phone thumb bar is reachable before selection, sheets restore focus and desktop keeps a column',()=>{
  const {doc,host,resize}=dom(true);mountChoicePipeline(host,{steps:sample});
  const pager=host.querySelector('.jp-pager');assert.equal(pager.parentElement,host);
  const step=host.querySelector('.jp-step');step.click();
  const sheet=host.querySelector('.jp-inspector');assert.equal(sheet.modal,true);assert.equal(pager.parentElement,sheet);
  sheet.close();assert.equal(doc.activeElement,step);assert.equal(pager.parentElement,host);
  resize(false);assert.equal(sheet.open,true);assert.equal(sheet.modal,false);assert.equal(pager.parentElement,sheet);
});
test('palette gates unavailable commands and announces filtered counts',()=>{
  const {doc,host}=dom();const opener=doc.createElement('button');host.append(opener);let called=0;
  const {open,dialog}=mountCommandPalette(doc,opener,[{label:'Run',reason:'not declared',run:()=>called++},{label:'Map',run:()=>called++}]);
  open();const input=dialog.querySelector('input');input.dispatch('keydown',{key:'Enter'});assert.equal(called,0);assert.equal(dialog.open,true);
  input.value='map';input.dispatch('input');assert.equal(dialog.querySelector('[role="status"]').textContent,'1 command');
  input.dispatch('keydown',{key:'Enter'});assert.equal(called,1);assert.equal(dialog.open,false);assert.equal(doc.activeElement,opener);
});
test('group attention auto-opens and selection reveals a hidden optional step',()=>{
  const {host}=dom();const steps=[...sample,{id:'optional',word:'OK',group:'opt',optional:true},{id:'blocked',word:'BLOCKED',group:'attention'}];
  const api=mountChoicePipeline(host,{steps});const groups=host.querySelectorAll('.jp-group');
  assert.equal(groups[0].hidden,true);assert.equal(groups[1].hidden,false);assert.equal(host.querySelectorAll('.jp-gap')[1].disabled,true);
  api.select(steps[3]);assert.equal(groups[0].hidden,false);
});
test('unavailable events never produce a tick; declared receipts update the live count',()=>{
  for(const declared of [false,true]){const {host}=dom();const api=mountChoicePipeline(host,{steps:sample,eventStreamDeclared:declared});api.receiveEvent({runId:'run',id:'event'});assert.equal(host.querySelector('.jp-live').textContent.includes('1 this run'),declared);api.cleanup();}
});


test('CSS cannot override the recorded palette after the generated block', () => {
  const css=readFileSync('web/jaryo.css','utf8');
  assert.deepEqual(checkChoiceCss(css),[]);
  assert.ok(checkChoiceCss(css+'\n:root { --j-accent: #C47A3A; }').length);
});
test('a live board refresh restores the phone sheet and local palette query without duplicate IDs', () => {
  const {doc,host}=dom(true);
  mountChoicePipeline(host,{steps:sample,selectedId:'a'});
  host.querySelector('.jp-step').click();
  const buttons=host.querySelector('.jp-pager').querySelectorAll('button');buttons.at(-1).click();
  const input=doc.querySelector('.jp-palette[open]').querySelector('input'); input.value='branch';input.dispatch('input');
  const resume=host.choiceViewState();
  assert.equal(resume.sheetOpen,true); assert.equal(resume.paletteOpen,true);
  mountChoicePipeline(host,{steps:sample,selectedId:'a',resume});
  assert.equal(host.querySelector('.jp-inspector').modal,true);
  assert.equal(doc.querySelector('.jp-palette[open]').querySelector('input').value,'branch');
  const ids=doc.querySelectorAll('[role="listbox"]').map(node=>node.id);
  assert.equal(new Set(ids).size,ids.length);
});


test('the shipped web module initializes its command palette before any user action', () => {
  const {doc}=dom();doc.documentElement={dataset:{}};doc.addEventListener=()=>{};
  const nav=doc.createElement('nav');nav.className='j-nav';doc.body.append(nav);
  const source=readFileSync('web/jaryo.mjs','utf8').replace(/^import .*;\n/gm,'').replace(/^export /gm,'');
  assert.doesNotThrow(()=>runInNewContext(source,{document:doc}));
  assert.equal(doc.querySelectorAll('.jp-palette').length,1);
  nav.querySelector('button').click();assert.equal(doc.querySelector('.jp-palette').open,true);
});

for (const delayed of [false,true]) for (const route of ['More','fork']) test(`desktop ${route} focuses its destination through ${delayed?'queued':'immediate'} close`,()=>{
  const {doc,host}=dom();doc.deferClose=delayed;
  mountChoicePipeline(host,{steps:sample,selectedId:'a'});
  if(route==='More') {
    host.querySelector('.jp-pager').querySelectorAll('button').at(-1).click();
    const input=doc.querySelector('.jp-palette[open]').querySelector('input');
    input.value='Branch B';input.dispatch('input');input.dispatch('keydown',{key:'Enter'});
  } else {
    host.querySelector('[data-direction="Next"]').click();
    doc.querySelector('.jp-branches').querySelector('button').click();
  }
  doc.flushClose();
  assert.ok(doc.activeElement===host.querySelectorAll('.jp-step')[1], 'focus lands on the chosen station');
  assert.equal(doc.activeElement.isConnected,true);
});

test('remount closes and removes a fork chooser owned by the previous component',()=>{
  const {doc,host}=dom();doc.deferClose=true;
  const selected=[];
  mountChoicePipeline(host,{steps:sample,selectedId:'a',onSelect:id=>selected.push(id)});
  host.querySelector('[data-direction="Next"]').click();
  const old=doc.querySelector('.jp-branches');
  mountChoicePipeline(host,{steps:sample,selectedId:'c'});
  assert.equal(old.open,false);
  assert.equal(old.isConnected,false);
  assert.equal(doc.querySelectorAll('.jp-branches').length,0);
  doc.flushClose();
  assert.equal(host.querySelectorAll('.jp-step')[2].getAttribute('aria-pressed'),'true');
  assert.deepEqual(selected,[]);
});

test('desktop global Cmd/Ctrl-K accepts a nonmodal inspector and respects a modal',async()=>{
  for(const modifier of ['metaKey','ctrlKey']) {
    const {doc,host}=dom();host.className='jk-shell';
    const button=doc.createElement('button');button.setAttribute('data-jk-commands','');host.append(button);
    const pipeline=doc.createElement('div');host.append(pipeline);
    mountChoicePipeline(pipeline,{steps:sample,selectedId:'a'});
    await mountKit(doc,{storage:{getItem:()=>null},matchMedia:()=>({matches:false}),search:''});
    const palettes=doc.querySelectorAll('.jp-palette');
    doc.dispatch('keydown',{key:'k',[modifier]:true});
    assert.equal(palettes.at(-1).open,true);
    palettes.at(-1).close();
    const modal=doc.createElement('dialog');doc.body.append(modal);modal.showModal();
    doc.dispatch('keydown',{key:'k',[modifier]:true});
    assert.equal(palettes.at(-1).open,false);
  }
});

function assertPaletteKey(mount,key,index) {
  const {doc,host}=dom();const opener=doc.createElement('button');host.append(opener);const ran=[];
  const {open,dialog}=mount(doc,opener,['Alpha','Beta','Gamma'].map(label=>({label,run:()=>ran.push(label)})));
  open();const input=dialog.querySelector('input');const options=dialog.querySelectorAll('[role="option"]');
  if(key==='Home')input.dispatch('keydown',{key:'End'});
  const before=input.getAttribute('aria-activedescendant');input.dispatch('keydown',{key});
  assert.notEqual(input.getAttribute('aria-activedescendant'),before);
  assert.equal(input.getAttribute('aria-activedescendant'),options[index].id);
  assert.deepEqual(options.map(n=>n.getAttribute('aria-selected')),options.map((_,i)=>String(i===index)));
  input.dispatch('keydown',{key:'Enter'});assert.deepEqual(ran,[['Alpha','Beta','Gamma'][index]]);
}
for(const [key,index] of [['ArrowDown',1],['ArrowUp',2],['Home',0],['End',2]]) {
  test(`palette ${key} moves active descendant and Enter runs that destination`,()=>assertPaletteKey(mountCommandPalette,key,index));
  test(`palette regression rejects removal of ${key} handling`,()=>{
    const source=readFileSync('jaryo/interactions.mjs','utf8').replace(/^export /gm,'');
    const original="['ArrowDown','ArrowUp','Home','End']";
    const replacement=original.replace(`'${key}'`,"'unused'");
    assert.ok(source.includes(original));
    const broken=runInNewContext(source.replace(original,replacement)+'; mountCommandPalette');
    assert.throws(()=>assertPaletteKey(broken,key,index),{code:'ERR_ASSERTION'});
  });
}

test('the shipped map view mounts choices beside the existing map and evidence controls',()=>{
  assert.match(readFileSync('web/monitor.html','utf8'),/src="\/monitor-map\.mjs"/);
  const {doc}=dom();let paintMap;let rerenders=0;
  for(const [tag,id] of [['div','monitorTabs'],['section','mapLedger'],['svg','map']]) {
    const node=doc.createElement(tag);node.id=id;doc.body.append(node);
    if(id==='monitorTabs')node.setAttribute('data-jaryo-tabs','1');
  }
  const map=doc.querySelector('#map');const point=doc.createElement('g');map.append(point);
  const context={document:doc,window:{addEventListener:(_,fn)=>paintMap=fn,__jaryoRerenderMap:()=>rerenders++},location:{search:''},...mapTabs,
    CustomEvent:class {constructor(type,options){Object.assign(this,{type},options);}},
    appendEvidence:(host)=>{const details=doc.createElement('details');details.className='j-evidence';host.append(details);}};
  const module=file=>readFileSync(file,'utf8').replace(/^import .*;\n/gm,'').replace(/^export /gm,'');
  runInNewContext(module('web/jaryo.mjs'),context);
  runInNewContext(module('web/monitor-map.mjs'),context);
  const detail={map:{nodes:[{id:'mac',machine:'mac',role:'machine',label:'Mac',health:'ok'},{id:'pc',machine:'pc',role:'machine',label:'PC',health:'not_measured'}],edges:[]},feedUnavailable:false};
  paintMap({detail});
  const ledger=doc.querySelector('#mapLedger');
  assert.ok(ledger.querySelector('.jp-spine'));
  assert.ok(ledger.querySelector('.jp-inspector'));
  assert.ok(ledger.querySelector('.jp-pager'));
  assert.ok(ledger.querySelector('.jp-ledger'));
  assert.ok(ledger.querySelector('.j-evidence'));
  assert.equal(map.children[0],point,'map drawing remains in its original container');
  assert.equal(ledger.querySelectorAll('.jp-step').length,2);
  ledger.querySelectorAll('.jp-step')[1].click();
  assert.equal(rerenders,0,'inspecting a station does not change the map filter');
  assert.match(ledger.textContent,/Dependencies not declared/);
  const more=ledger.querySelector('.jp-pager').querySelectorAll('button').at(-1);more.click();
  const input=doc.querySelector('.jp-palette[open]').querySelector('input');input.value='PC';input.dispatch('input');
  paintMap({detail});
  assert.equal(doc.querySelectorAll('.jp-palette').length,1,'refresh cleans the previous palette');
  assert.equal(doc.querySelector('.jp-palette[open]').querySelector('input').value,'PC');
  assert.equal(ledger.querySelectorAll('.jp-step')[1].getAttribute('aria-pressed'),'true');
  ledger.dispatch('click',{target:ledger.querySelector('[data-open-monitor]')});
  assert.equal(context.window.__jaryoMapTab,'pc');
  assert.equal(rerenders,1,'Open monitor retains the existing map navigation');
});

function liveMapFixture() {
  const {doc}=dom();let publish;
  for(const id of ['monitorTabs','mapLedger']) {
    const node=doc.createElement('div');node.id=id;doc.body.append(node);
    if(id==='monitorTabs')node.setAttribute('data-jaryo-tabs','1');
  }
  const context={document:doc,window:{addEventListener:(_,fn)=>publish=fn},location:{search:''},...mapTabs,
    CustomEvent:class {constructor(type,options){Object.assign(this,{type},options);}},appendEvidence:()=>{}};
  for(const file of ['web/jaryo.mjs','web/monitor-map.mjs'])
    runInNewContext(readFileSync(file,'utf8').replace(/^import .*;\n/gm,'').replace(/^export /gm,''),context);
  const detail={map:{nodes:[{id:'mac',machine:'mac',role:'machine',label:'Mac',health:'ok'},{id:'pc',machine:'pc',role:'machine',label:'PC',health:'ok'}],edges:[]},feedUnavailable:false};
  const refresh=()=>publish({detail});refresh();
  return {doc,ledger:doc.querySelector('#mapLedger'),refresh};
}
for(const [name,selected,target] of [
  ['unselected station',false,host=>host.querySelector('.jp-step')],
  ['unselected More',false,host=>host.querySelector('.jp-pager').querySelectorAll('button').at(-1)],
  ['unselected ledger',false,host=>host.querySelector('.jp-ledger-region')],
  ['station independent of selection',true,host=>host.querySelectorAll('.jp-step')[1]],
  ['Inspect control',true,host=>host.querySelector('.jp-primary')],
  ['Open monitor control',true,host=>host.querySelector('[data-open-monitor]')],
]) test(`live refresh preserves ${name} focus independently of selection`,()=>{
  const {doc,ledger,refresh}=liveMapFixture();
  if(selected)ledger.querySelector('.jp-step').click();
  const old=target(ledger);old.focus();refresh();
  assert.equal(old.isConnected,false);
  assert.ok(doc.activeElement===target(ledger),'focus reaches the equivalent control in the replacement host');
  assert.equal(doc.activeElement.isConnected,true);
  assert.deepEqual(ledger.querySelectorAll('.jp-step').map(node=>node.getAttribute('aria-pressed')),selected?['true','false']:['false','false']);
  assert.equal(ledger.dataset.selectedStep,selected?'mac':undefined,'restoring focus must not select a station');
  const outside=doc.createElement('button');doc.body.append(outside);outside.focus();refresh();
  assert.ok(doc.activeElement===outside,'a refresh must not steal external focus');
});

for(const [route,selectedId,destination] of [['Inspect','b','b'],['IN neighbour','b','a'],['OUT neighbour','a','b']])
  test(`${route} retains keyboard focus after replacing inspector controls`,()=>{
    for(const phone of [false,true]) {
      const {doc,host}=dom(phone);mountChoicePipeline(host,{steps:sample});
      host.querySelectorAll('.jp-step').find(node=>node.dataset.station===selectedId).click();
      const button=route==='Inspect'?host.querySelector('.jp-primary')
        :host.querySelector('.jp-inspector-content').querySelectorAll('button').find(node=>node.textContent.startsWith(route.slice(0,3).trim()));
      assert.ok(button);button.focus();button.click();
      assert.equal(button.isConnected,false,'activation replaces the original control');
      const station=host.querySelectorAll('.jp-step').find(node=>node.dataset.station===destination);
      assert.equal(station.getAttribute('aria-pressed'),'true');
      assert.ok(doc.activeElement===(phone?host.querySelector('.jp-inspector-title'):station),'focus reaches a connected inspection destination');
      assert.equal(doc.activeElement.isConnected,true);
    }
  });

for(const modifier of ['metaKey','ctrlKey'])test(`same-host remount removes stale ${modifier} command handlers`,()=>{
  const {doc,host}=dom();
  mountChoicePipeline(host,{steps:sample});
  const old=doc.querySelector('.jp-palette');
  const current=mountChoicePipeline(host,{steps:[{id:'new',name:'Current station',word:'OK'}]});
  assert.equal(old.isConnected,false);
  assert.throws(()=>old.showModal(),{name:'InvalidStateError'},'the DOM fixture rejects disconnected modal dialogs');
  const press=()=>host.dispatch('keydown',{key:'k',[modifier]:true});
  assert.doesNotThrow(press);
  const palette=doc.querySelector('.jp-palette[open]');assert.ok(palette);
  assert.equal(doc.querySelectorAll('.jp-palette').length,1);
  assert.match(palette.textContent,/Current station/);
  assert.equal(old.open,false);
  current.cleanup();assert.doesNotThrow(press);
  assert.equal(doc.querySelectorAll('.jp-palette').length,0,'cleanup removes both palette and shortcut');
});
