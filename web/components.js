const $=id=>document.getElementById(id);
let timer=null,nisiPosting=false,jevPosting=false,nisiIdentity=null,nisiAvailable=false;
let pendingReview=null;
function openComponentReview({title,target,effects,duration},confirm,opener=document.activeElement){
  const dialog=$('actionReview');if(dialog.open)return;
  $('actionReviewHeading').textContent=`◆ REVIEW · ${title}`;
  $('actionReviewTarget').textContent=target;$('actionReviewEffects').textContent=effects;
  $('actionReviewDuration').textContent=duration;
  pendingReview={confirm,opener};dialog.showModal();$('actionReviewCancel').focus();
}
function closeComponentReview(){
  const pending=pendingReview;pendingReview=null;
  if($('actionReview').open)$('actionReview').close();
  if(pending?.opener?.isConnected!==false)pending?.opener?.focus({preventScroll:true});
  return pending;
}
function confirmComponentReview(){const pending=closeComponentReview();if(pending)pending.confirm();}
$('actionReviewCancel').addEventListener('click',()=>closeComponentReview());
$('actionReviewConfirm').addEventListener('click',confirmComponentReview);
$('actionReview').addEventListener('cancel',event=>{event.preventDefault();closeComponentReview();});
$('actionReview').addEventListener('close',()=>{if(pendingReview)closeComponentReview();});
const quote=value=>"'"+String(value).replaceAll("'","'\\''")+"'";
async function refresh(){
  clearTimeout(timer);nisiAvailable=false;
  try{
    const response=await fetch('/api/components',{cache:'no-store',signal:AbortSignal.timeout(4000)});
    if(response.status===501){const refusal=await response.json().catch(()=>null);const message=refusal&&typeof refusal.message==='string'?refusal.message:'Not available in the Windows edition yet.';$('integrity').textContent='Unavailable';$('runtime').textContent=message;$('result').textContent=message;$('check').disabled=true;return;}
    if(!response.ok)throw new Error('unavailable');
    const data=await response.json(),verified=data.nisi?.integrity==='verified',running=data.check?.state==='running';
    const version=typeof data.nisi?.version==='string'&&/^\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(data.nisi.version)?data.nisi.version:null,tag=document.querySelector('#nisiHeading span');if(tag&&version)tag.textContent='v'+version;$('integrity').textContent=verified?(version?'Verified · public '+version+' payload':'Verified · public payload'):'Unavailable · package verification failed';
    $('runtime').textContent=data.check?.state==='passed'?`Node ${data.check.nodeVersion} · Apple silicon verified`:data.node?.found?'Node found · run self-check to verify':'Node.js 22+ required';
    nisiIdentity=verified?JSON.stringify([data.nisi?.version,data.nisi?.entry]):null;nisiAvailable=verified&&!running;
    $('check').disabled=nisiPosting||!verified||running;
    $('check').textContent=running?'Checking…':'Review Nisi self-check…';
    $('result').textContent=data.check?.message||'Self-check not run.';
    $('command').textContent=(data.node?.path?quote(data.node.path):'node')+' '+quote(data.nisi?.entry||'')+' --version';
    if(running)timer=setTimeout(refresh,750);
  }catch{
    nisiIdentity=null;
    $('integrity').textContent='Unknown · observer unavailable';
    $('runtime').textContent='Unknown · observer unavailable';
    $('result').textContent='The local observer is unavailable. Reopen Components from the running app.';
    $('check').disabled=true;
  }
}
async function requestNisiCheck(identity){
  if(nisiPosting)return;nisiPosting=true;let message=null;
  try{
    await refresh();
    if(!nisiAvailable||identity===null||identity!==nisiIdentity){message='The package state changed. Review its current status before retrying.';return;}
    const response=await fetch('/api/components/nisi/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'self-check'}),signal:AbortSignal.timeout(4000)});
    if(!response.ok)throw new Error('unavailable');
  }catch{message='The self-check could not start. Reload this page to try again.';}
  finally{nisiPosting=false;await refresh();if(message)$('result').textContent=message;}
}
$('check').addEventListener('click',()=>{
  if($('check').disabled||nisiPosting)return;
  const identity=nisiIdentity;
  openComponentReview({title:'Run Nisi self-check',target:'This Mac · bundled Nisi package',effects:'Runs the installed Node.js package self-check and records its result. It makes no model calls and does not run a coding task.',duration:'Completion time is not measured. Closing this view does not cancel the check.'},()=>requestNisiCheck(identity),$('check'));
});
refresh();

let jevTimer=null,jevAvailable=false;
const bridge=window.webkit?.messageHandlers?.monitor;
$('jevConfigure').disabled=!bridge;
if(!bridge)$('jevConfigure').title='Open Components in the installed AGIW app to use its secure Keychain dialog.';
async function refreshJev(){
  clearTimeout(jevTimer);jevAvailable=false;
  try{
    const response=await fetch('/api/components/jev',{cache:'no-store',signal:AbortSignal.timeout(6000)});
    if(response.status===501){jevAvailable=false;const refusal=await response.json().catch(()=>null);const message=refusal&&typeof refusal.message==='string'?refusal.message:'Not available in the Windows edition yet.';$('jevStatus').textContent=message;$('jevCheck').disabled=true;return;}
    if(!response.ok)throw new Error('unavailable');
    const data=await response.json();
    $('jevStatus').textContent=data.configured?'API key saved in Keychain.':'Not configured. '+(!bridge?'Open this page in the installed AGIW app to set it up.':'Set up a connection with your own TypeSafe key.');
    $('jevCheck').disabled=jevPosting||!data.configured||data.state==='checking';
    jevAvailable=data.configured===true&&data.state!=='checking';
    $('jevResult').textContent=data.message||'No connection check has run.';
    if(data.state==='checking')jevTimer=setTimeout(refreshJev,750);
  }catch{jevAvailable=false;$('jevStatus').textContent='Jev connection status is unavailable.';$('jevCheck').disabled=true;}
}
$('jevConfigure').addEventListener('click',()=>{bridge?.postMessage({action:'jev-configure'});});
window.addEventListener('focus',refreshJev);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshJev();});
async function requestJevCheck(){
  if(jevPosting)return;jevPosting=true;let message=null;
  try{
    await refreshJev();
    if(!jevAvailable){message='The connection state changed. Review its current status before retrying.';return;}
    const response=await fetch('/api/components/jev/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'connection-check'}),signal:AbortSignal.timeout(6000)});
    if(!response.ok)throw new Error('unavailable');
  }catch{message='The connection check could not start. Reload this page to try again.';}
  finally{jevPosting=false;await refreshJev();if(message)$('jevResult').textContent=message;}
}
$('jevCheck').addEventListener('click',()=>{
  if($('jevCheck').disabled||jevPosting)return;
  openComponentReview({title:'Check Jev connection',target:'Configured TypeSafe Jev connection',effects:'Sends the saved API key and one fixed synthetic classification test to TypeSafe over HTTPS. The provider receives your IP address and request metadata; usage charges may apply. Sends no files, task prompts or model inventory and does not enable task routing.',duration:'Completion time is not measured. The existing connection-check deadline remains in force; closing this view does not cancel the check.'},requestJevCheck,$('jevCheck'));
});
refreshJev();
