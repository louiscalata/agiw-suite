const $=id=>document.getElementById(id);
let timer=null;
const quote=value=>"'"+String(value).replaceAll("'","'\\''")+"'";
async function refresh(){
  clearTimeout(timer);
  try{
    const response=await fetch('/api/components',{cache:'no-store',signal:AbortSignal.timeout(4000)});
    if(!response.ok)throw new Error('unavailable');
    const data=await response.json(),verified=data.nisi?.integrity==='verified',running=data.check?.state==='running';
    $('integrity').textContent=verified?'Verified · public 0.2.0 payload':'Unavailable · package verification failed';
    $('runtime').textContent=data.check?.state==='passed'?`Node ${data.check.nodeVersion} · Apple silicon verified`:data.node?.found?'Node found · run self-check to verify':'Node.js 22+ required';
    $('check').disabled=!verified||running;
    $('check').textContent=running?'Checking…':'Run Nisi self-check';
    $('result').textContent=data.check?.message||'Self-check not run.';
    $('command').textContent=(data.node?.path?quote(data.node.path):'node')+' '+quote(data.nisi?.entry||'')+' --version';
    if(running)timer=setTimeout(refresh,750);
  }catch{
    $('result').textContent='The local observer is unavailable. Reopen Components from the running app.';
    $('check').disabled=true;
  }
}
$('check').addEventListener('click',async()=>{
  $('check').disabled=true;
  try{
    const response=await fetch('/api/components/nisi/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'self-check'}),signal:AbortSignal.timeout(4000)});
    if(!response.ok)throw new Error('unavailable');
    await refresh();
  }catch{$('result').textContent='The self-check could not start. Reload this page to try again.';}
});
refresh();

let jevTimer=null;
const bridge=window.webkit?.messageHandlers?.monitor;
$('jevConfigure').disabled=!bridge;
if(!bridge)$('jevConfigure').title='Open Components in the installed AGIW app to use its secure Keychain dialog.';
async function refreshJev(){
  clearTimeout(jevTimer);
  try{
    const response=await fetch('/api/components/jev',{cache:'no-store',signal:AbortSignal.timeout(6000)});
    if(!response.ok)throw new Error('unavailable');
    const data=await response.json();
    $('jevStatus').textContent=data.configured?'API key saved in Keychain.':'Not configured. '+(!bridge?'Open this page in the installed AGIW app to set it up.':'Set up a connection with your own TypeSafe key.');
    $('jevCheck').disabled=!data.configured||data.state==='checking';
    $('jevResult').textContent=data.message||'No connection check has run.';
    if(data.state==='checking')jevTimer=setTimeout(refreshJev,750);
  }catch{$('jevStatus').textContent='Jev connection status is unavailable.';$('jevCheck').disabled=true;}
}
$('jevConfigure').addEventListener('click',()=>{bridge?.postMessage({action:'jev-configure'});});
window.addEventListener('focus',refreshJev);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshJev();});
$('jevCheck').addEventListener('click',async()=>{
  $('jevCheck').disabled=true;
  try{
    const response=await fetch('/api/components/jev/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'connection-check'}),signal:AbortSignal.timeout(6000)});
    if(!response.ok)throw new Error('unavailable');
    await refreshJev();
  }catch{$('jevResult').textContent='The connection check could not start. Reload this page to try again.';}
});
refreshJev();
