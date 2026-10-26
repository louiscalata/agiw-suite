import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {layout,onlineMode,appDefinitions} from './production.mjs';
import * as usage from '../usage-format.mjs';
import * as modelControl from '../web/model-control-view.mjs';
const definitions=appDefinitions
  .replace(/^import .*;\r?\n/gm,'').split("$('pause').addEventListener")[0];
class FakeClassList {
  constructor(node) { this.node = node; }
  get names() { return new Set(String(this.node.className).split(/\s+/).filter(Boolean)); }
  add(...names) { const set = this.names; names.forEach(n => set.add(n)); this.node.className = [...set].join(' '); }
  remove(...names) { const set = this.names; names.forEach(n => set.delete(n)); this.node.className = [...set].join(' '); }
  contains(name) { return this.names.has(name); }
  toggle(name, force) { const on = force === undefined ? !this.contains(name) : Boolean(force); on ? this.add(name) : this.remove(name); return on; }
}

class FakeElement {
  constructor(doc, tag, id = '') {
    Object.assign(this, { doc, tagName: tag.toUpperCase(), id, children: [], parentNode: null, dataset: {}, style: {},
      attributes: {}, hidden: false, scrollTop: 0, listeners: {}, className: '', ownText: '' });
    this.classList = new FakeClassList(this);
  }
  get textContent() { return this.children.length ? this.children.map(c => c.textContent).join('') : this.ownText; }
  set textContent(value) { this.detachChildren(); this.ownText = String(value); }
  // A browser moves focus to <body> when the focused element leaves the document.
  detachChildren() {
    for (const child of this.children) { if (child.contains(this.doc.activeElement)) this.doc.activeElement = this.doc.body; child.parentNode = null; }
    this.children = [];
  }
  append(...nodes) {
    for (let node of nodes) {
      if (typeof node === 'string') { const text = new FakeElement(this.doc, '#text'); text.ownText = node; node = text; }
      if (node.parentNode) node.parentNode.children = node.parentNode.children.filter(child => child !== node);
      node.parentNode = this; this.children.push(node);
    }
  }
  // Replacing an element's children resets the scroll of the box that actually scrolls: the nearest
  // ancestor marked as a scroller (the Live activity section), or the element itself.
  replaceChildren(...nodes) {
    this.detachChildren();
    let scroller = this; while (scroller && !scroller.scroller) scroller = scroller.parentNode;
    (scroller || this).scrollTop = 0; this.append(...nodes);
  }
  get lastElementChild() { return this.children.at(-1) || null; }
  contains(node) { for (let n = node; n; n = n.parentNode) if (n === this) return true; return false; }
  matches(selector) { return selector.startsWith('.') ? this.classList.contains(selector.slice(1)) : this.tagName === selector.toUpperCase(); }
  closest(selector) { for (let n = this; n; n = n.parentNode) if (n.matches(selector)) return n; return null; }
  querySelectorAll(selector) { const out = []; const walk = n => n.children.forEach(c => { if (c.matches(selector)) out.push(c); walk(c); }); walk(this); return out; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.doc.activeElement = this; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  click() { for (const listener of this.listeners.click || []) listener({ target: this }); }
  getBoundingClientRect() { return { left: 0, top: 0, right: 820, bottom: 604, width: 820, height: 604 }; }
}

export function harness({ width = 1150, extra = {} } = {}) {
  const doc = { elements: new Map() };
  doc.body = new FakeElement(doc, 'body');
  doc.activeElement = doc.body;
  doc.createElement = tag => new FakeElement(doc, tag);
  doc.createTextNode = text => { const node = new FakeElement(doc, '#text'); node.ownText = String(text); return node; };
  doc.getElementById = id => {
    if (!doc.elements.has(id)) { const node = new FakeElement(doc, 'div', id); doc.body.append(node); doc.elements.set(id, node); }
    return doc.elements.get(id);
  };
  for (const id of ['drawer', 'onlineCodeDetails', 'fixInferenceDetails', 'activityDetails', 'aboutPanel', 'modelActionDock', 'memoryBanner', 'memoryBannerMark', 'fixInferencePlainSteps', 'drawerFixButton']) doc.getElementById(id).hidden = true;
  for (const id of ['vitalMac', 'vitalPc', 'vitalRoute', 'vitalActivity']) doc.getElementById(id).append(doc.createElement('strong'), doc.createElement('small'));
  doc.getElementById('sidebarToggle').append(doc.createElement('span'), doc.createElement('span'));
  for (const scope of ['core', 'all']) {
    const button = doc.createElement('button');
    button.dataset.modelScope = scope;
    doc.getElementById('modelScopeControls').append(button);
  }
  doc.getElementById('activityDetails').append(doc.getElementById('activityList'), doc.getElementById('activityAnnounce'));
  // The section scrolls (overflow-y:auto); the list inside it grows.
  doc.getElementById('activityDetails').scroller = true;
  const view = { width };
  const window = { matchMedia: query => ({ matches: Number(/max-width:\s*(\d+)px/.exec(query)?.[1] ?? 0) >= view.width }) };
  const api = runInNewContext(`${definitions}
;({renderActivity,renderVitals,renderSummary,vitalsInput,syncCompactUI,toggleActivity,toggleAbout,closeCompactSidebar,setSidebarOpen,
  rebuildPanel,disclosure,onlineCodeSummary,liveNow,renderInspector,modelControlPollWanted,onlineCodePollWanted,pollOnlineCodeAction,
  renderModelScope,renderList,setModelScope,
  renderAutoUnload,pollAutoUnload,requestAutoUnload,get autoUnloadState(){return autoUnloadState;},
  requestOnlineCodeAction,updateOnlineCodeMode,updateInferenceFix,openFixInference,closeFixInference,renderMemoryBanner,dismissMemoryBanner,
  get memoryDismissed(){return memoryDismissed;},
  get graph(){return graph;},
  // The rail without the map: selecting a node or closing the drawer only moves panels (the map's SVG is not faked here).
  spyRail(){const picked=[];nodeSelect=id=>{picked.push(id);selected=id;$('drawer').hidden=false;makeGraph();renderInspector();};closeDrawer=()=>{$('drawer').hidden=true;selected=null;inspectorNode=null;};fitMap=()=>{};revealSelected=()=>{};return picked;},
  setTraceView(){view='runs';},select(id){selected=id;makeGraph();},render(){makeGraph();renderInspector();},nodeSubtitle(id){return graph.nodes.find(n=>n.id===id)?.subtitle;},
  spyScope(){render=()=>{renderModelScope();makeGraph();renderList();};},
  scopeProjection(){renderModelScope();makeGraph();renderList();return graph.nodes.filter(n=>n.kind==='model').map(n=>n.model.id);},
  refreshEvidence(){renderGraph=()=>{};fitMap=()=>{};render();},
  get selected(){return selected;},
  setModelControl(status,posting=false){modelControlStatus=status;modelControlPosting=posting;},
  setAction(status){onlineCodeActionStatus=status;},
  setPaused(value){paused=value;},
  setFeed(value,isConnected=true){snapshot=value;connected=isConnected;},
  get lastPanel(){return lastEvidencePanel;},set lastPanel(value){lastEvidencePanel=value;},
  inspect(node){const body=document.createElement('main');inspectorBody(node,body);return body;},
  graphState(){return graph;},
  modelRows(){return models();},
  spyOpen(){const opened=[];openActivityRow=row=>opened.push(row.key);return opened;}})`,
  { ...layout, ...usage, ...onlineMode, ...modelControl, document: doc, window, performance: { now: () => 0 }, ...extra });
  return { api, doc, $: id => doc.getElementById(id), view };
}

