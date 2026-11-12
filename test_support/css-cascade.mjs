// Bounded source-cascade probe for the UI fixtures below, not a rendering engine.
// Resolves source order, selector specificity, !important and the relevant shorthands.
export function cssRules(source) {
  const out=[];
  function walk(text,media='') {
    let offset=0;
    while(offset<text.length) {
      const open=text.indexOf('{',offset);if(open<0)break;
      const head=text.slice(offset,open).trim();let end=open+1,depth=1;
      while(depth&&end<text.length){if(text[end]==='{')depth++;else if(text[end]==='}')depth--;end++;}
      const body=text.slice(open+1,end-1);
      if(head.startsWith('@media'))walk(body,head.slice(6).trim());
      else if(!head.startsWith('@'))out.push({media,selectors:head.split(',').map(s=>s.trim()),body});
      offset=end;
    }
  }
  walk(source.replace(/\/\*[\s\S]*?\*\//g,''));return out;
}
function compound(selector,node) {
  if(!node)return false;
  selector=selector.replace(/:where\(([^()]*)\)/g,'$1');
  let accepted=true;
  selector=selector.replace(/:not\(([^()]*)\)/g,(_,part)=>{if(compound(part,node))accepted=false;return '';});
  if(!accepted||selector.includes(':'))return false; // No pseudo-element/interaction in these fixtures.
  selector=selector.replace(/\[([\w-]+)(?:="([^"]*)")?\]/g,(_,key,value)=>{
    if(value===undefined?node.attrs?.[key]==null:node.attrs?.[key]!==value)accepted=false;return '';
  });
  selector=selector.replace(/\.([\w-]+)/g,(_,name)=>{if(!node.classes?.includes(name))accepted=false;return '';});
  selector=selector.replace(/#([\w-]+)/g,(_,id)=>{if(node.id!==id)accepted=false;return '';});
  return accepted&&(!selector||selector==='*'||selector===node.tag);
}
function matches(selector,lineage) {
  const parts=selector.replace(/\s*>\s*/g,' > ').split(/\s+/);let index=lineage.length-1;
  if(!compound(parts.pop(),lineage[index--]))return false;
  while(parts.length) {
    const part=parts.pop();
    if(part==='>'){if(!compound(parts.pop(),lineage[index--]))return false;}
    else {while(index>=0&&!compound(part,lineage[index]))index--;if(index--<0)return false;}
  }
  return true;
}
function specificity(selector) {
  const s=selector.replace(/:where\([^()]*\)/g,'').replace(/:not\(([^()]*)\)/g,'$1');
  const ids=(s.match(/#[\w-]+/g)||[]).length;
  const classes=(s.match(/\.[\w-]+|\[[^\]]+\]|:[\w-]+/g)||[]).length;
  const types=(s.replace(/#[\w-]+|\.[\w-]+|\[[^\]]+\]|:[\w-]+/g,'').match(/\b[a-z][\w-]*\b/g)||[]).length;
  return ids*10000+classes*100+types;
}
function expand(property,value) {
  const out={[property]:value};
  if(property==='padding'||property==='border-style'||property==='border-width'||property==='border-color') {
    const [top,right=top,bottom=top,left=right]=value.split(/\s+/);
    for(const [i,side] of ['top','right','bottom','left'].entries())out[property==='padding'?`padding-${side}`:`border-${side}-${property.slice(7)}`]=[top,right,bottom,left][i];
  }
  if(/^border(?:-(top|right|bottom|left))?$/.test(property)) {
    const width=value.match(/(?:^|\s)(\d+(?:px)?)(?:\s|$)/)?.[1]||'medium';
    const style=value.match(/\b(none|solid|double|dashed|dotted)\b/)?.[1]||'none';
    const color=value.match(/var\([^)]+\)|#[\da-f]+|HighlightText|CanvasText|transparent|currentColor/i)?.[0]||'currentColor';
    for(const side of property==='border'?['top','right','bottom','left']:[property.slice(7)])
      Object.assign(out,{[`border-${side}-width`]:width,[`border-${side}-style`]:style,[`border-${side}-color`]:color});
  }
  if(property==='background')out['background-color']=value;
  return out;
}
export function cascadeStyle(css,lineage,{forced=false}={}) {
  const winners=new Map();
  for(const rule of cssRules(css)) {
    if(rule.media&&!(forced&&rule.media==='(forced-colors: active)'))continue;
    for(const selector of rule.selectors) {
      if(!matches(selector,lineage))continue;
      for(const declaration of rule.body.split(';')) {
        const colon=declaration.indexOf(':');if(colon<0)continue;
        const property=declaration.slice(0,colon).trim(),raw=declaration.slice(colon+1).trim();
        const score=specificity(selector)+(/!important/.test(raw)?1000000:0);
        for(const [key,value] of Object.entries(expand(property,raw.replace(/\s*!important/,''))))
          if(!winners.has(key)||score>=winners.get(key).score)winners.set(key,{score,value});
      }
    }
  }
  return Object.fromEntries([...winners].map(([key,{value}])=>[key,value]));
}
