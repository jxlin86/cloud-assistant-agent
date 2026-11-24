'use strict';
const $ = id => document.getElementById(id);
let config, csrf, selected = null, cursor = 0, activePrompt = null, pollTimer = null;
let activeCloud=null;
const cloudDrafts={};
const compartmentLookups=new Map(), compartmentNames=new Map();
let compartmentTimer=null;
function compartmentKey(profile,region,ocid) {return JSON.stringify([profile,region,ocid]);}
function findCompartment(profile,region,ocid) {
  const key=compartmentKey(profile,region,ocid);
  if(!compartmentLookups.has(key)) {
    if(compartmentLookups.size>=128){compartmentLookups.clear();compartmentNames.clear();}
    const query=new URLSearchParams({profile,region,ocid});
    compartmentLookups.set(key,api('/api/oci/compartment?'+query).then(value=>{
      compartmentNames.set(key,value.name);return value;
    }).catch(()=>{setTimeout(()=>compartmentLookups.delete(key),30000);return null;}));
  }
  return compartmentLookups.get(key);
}
async function updateCompartmentName() {
  clearTimeout(compartmentTimer);
  const label=$('compartment-name'); label.hidden=activeCloud!=='oci';
  if(activeCloud!=='oci') return;
  const profile=$('profile').value.trim(),region=$('region').value.trim(),ocid=$('compartment').value.trim();
  const key=compartmentKey(profile,region,ocid);
  if(!/^ocid1\.(compartment|tenancy)\.[a-z0-9.]+$/.test(ocid)){label.textContent='Enter an OCID to show its name.';return;}
  label.textContent='Looking up compartment name…';
  const value=await findCompartment(profile,region,ocid);
  if(activeCloud!=='oci' || key!==compartmentKey($('profile').value.trim(),$('region').value.trim(),$('compartment').value.trim())) return;
  label.textContent=value?'Name: '+value.name:'Name unavailable · OCID retained';
}
function renderTaskScope(job) {
  const cloud=job.cloud || 'oci', scope=job.scope || job.compartment_id || '';
  const key=compartmentKey(job.profile,job.region,scope);
  const name=cloud==='oci'?(job.compartment_name || compartmentNames.get(key)):'';
  $('task-scope').textContent=`${cloudLabels[cloud]}  /  ${job.profile}  /  ${job.region}  ·  ${name?name+' ('+scope+')':scope}  ·  ${new Date(job.created_at).toLocaleString()}`;
  if(cloud==='oci' && !name) findCompartment(job.profile,job.region,scope).then(value=>{
    if(value && selected===job.id) renderTaskScope({...job,compartment_name:value.name});
  });
}
const cloudLabels={oci:'OCI',azure:'Azure',gcp:'Google Cloud',aws:'AWS'};
const examples={oci:[['List databases','List all databases in the selected compartment.'],['Check tenancy','Show which tenancy is connected.'],['Check Vault status','List vaults in the selected compartment.']],
  azure:[['List virtual machines','List virtual machines in the selected subscription.'],['List resource groups','List resource groups in the selected subscription.'],['List databases','List SQL servers in the selected subscription.']],
  gcp:[['List virtual machines','List Compute Engine instances in the selected project.'],['List databases','List Cloud SQL instances in the selected project.'],['List clusters','List GKE clusters in the selected project.']],
  aws:[['List EC2 instances','List EC2 instances in the selected account and region.'],['List databases','List RDS database instances in the selected account and region.'],['Check account','Show which AWS account is connected.']]};
function changeCloud() {
  if(activeCloud) cloudDrafts[activeCloud]={profile:$('profile').value,region:$('region').value,scope:$('compartment').value};
  activeCloud=$('cloud').value;
  const provider=config.providers[activeCloud], draft=cloudDrafts[activeCloud] || provider.defaults;
  $('profile-options').replaceChildren();
  for(const name of provider.profiles){const option=document.createElement('option');option.value=name;$('profile-options').append(option);}
  $('profile').value=draft.profile; $('profile').disabled=activeCloud==='azure';
  $('region').value=draft.region; $('compartment').value=draft.scope;
  $('profile-label').textContent={oci:'OCI profile',aws:'AWS profile',azure:'Azure CLI session',gcp:'gcloud configuration'}[activeCloud];
  $('scope-label').textContent={oci:'Compartment name and OCID',aws:'AWS account ID (12 digits)',azure:'Azure subscription ID',gcp:'Google Cloud project ID'}[activeCloud];
  $('cloud-mark').textContent={oci:'OCI',azure:'AZ',gcp:'GCP',aws:'AWS'}[activeCloud];
  $('workspace-cloud').textContent=provider.label+' Operations';
  $('cloud-connection-note').textContent=provider.available?`${provider.cli} CLI detected. The selected cloud connection is checked when you run a task.`:
    `${provider.cli} CLI is not installed or its launcher is unsupported. Install and sign in locally, then restart the service. Plan only remains available.`;
  document.querySelectorAll('[data-example]').forEach((button,index)=>{const [label,request]=examples[activeCloud][index];button.textContent=label;button.dataset.example=request;});
  $('request').placeholder=examples[activeCloud][0][1];
  updateCompartmentName();
}
const finals = new Set(['succeeded','planned','failed','blocked','cancelled','partially_completed','interrupted']);
function showError(message) { $('error').textContent = message; $('error').hidden = !message; }
async function api(path, options = {}) {
  const response = await fetch(path, {credentials:'same-origin', ...options, headers:{'Content-Type':'application/json','X-CSRF-Token':csrf || '', ...(options.headers || {})}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function statusName(value) { return value.replaceAll('_',' '); }
function renderUsage(usage) {
  const reported=usage && usage.requests>usage.missing_usage_requests;
  for(const [id,key] of [['input-tokens','input_tokens'],['output-tokens','output_tokens'],['total-tokens','total_tokens']]) {
    $(id).textContent=reported?usage[key].toLocaleString():'—';
  }
  $('llm-calls').textContent=usage?usage.requests.toLocaleString():'—';
  $('token-usage-note').textContent=!usage?'No usage recorded for this task.':
    !usage.requests?'Waiting for LLM usage.':usage.missing_usage_requests?
      `Reported usage only · ${usage.missing_usage_requests} call(s) without token counts.`:
      'Provider-reported totals for this task, including tool turns and plan repairs.';
}
function renderCommand(event) {
  const data=event.payload;
  const card=document.createElement('section'); card.className='command-card';
  const heading=document.createElement('h3'); heading.textContent=`Command output · ${data.category} · exit ${data.exit_code}`;
  const command=document.createElement('pre'); command.className='command-line'; command.textContent=data.command;
  const label=document.createElement('h4'); label.textContent='Readable output';
  const output=document.createElement('pre'); output.className='command-output'; output.textContent=event.text;
  const details=document.createElement('details'); details.className='command-json';
  const summary=document.createElement('summary'); summary.textContent='JSON command and output';
  const json=document.createElement('pre'); json.textContent=JSON.stringify(data,null,2);
  details.append(summary,json); card.append(heading,command,label,output,details); return card;
}
function resetTask() {
  selected = null; cursor = 0; activePrompt = null; clearTimeout(pollTimer);
  $('task-panel').hidden = true; $('welcome').hidden = false; $('reply').value = ''; $('input-panel').hidden = true;
  document.querySelectorAll('.history-item').forEach(item => item.classList.remove('selected'));
  $('request').focus();
}
async function refreshHistory() {
  const data = await api('/api/jobs');
  $('task-count').textContent = data.jobs.length;
  const list = $('history'); list.replaceChildren();
  if (!data.jobs.length) { const p=document.createElement('p'); p.className='empty-history'; p.textContent='Your tasks will appear here.'; list.append(p); }
  for (const job of data.jobs) {
    const button=document.createElement('button'); button.className='history-item' + (job.id===selected?' selected':'');
    const title=document.createElement('span'); title.className='history-title'; title.textContent=job.request; button.title=job.request;
    const meta=document.createElement('span'); meta.className='history-meta';
    const dot=document.createElement('span'); dot.className='mini-dot '+job.status;
    const label=document.createElement('span'); label.textContent=statusName(job.status)+' · '+new Date(job.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    meta.append(dot,label); button.append(title,meta); button.onclick=()=>selectTask(job.id); list.append(button);
  }
}
async function selectTask(id) {
  selected=id; cursor=0; activePrompt=null; clearTimeout(pollTimer); $('activity').replaceChildren(); $('reply').value='';
  renderUsage(null);
  $('welcome').hidden=true; $('task-panel').hidden=false; $('input-panel').hidden=true;
  await refreshHistory(); await pollTask();
}
function renderPrompt(pending) {
  if (!pending) { activePrompt=null; $('input-panel').hidden=true; $('reply').value=''; return; }
  if (activePrompt?.id===pending.id) return;
  activePrompt=pending; $('input-panel').hidden=false; $('reply').value='';
  const approval=pending.kind==='approval', secret=pending.kind==='secret';
  $('input-kicker').textContent=approval?'APPROVAL REQUIRED':secret?'HIDDEN INPUT REQUIRED':'CLARIFICATION NEEDED';
  $('input-title').textContent=approval?'Review and accept this plan':secret?'Enter the required secret':'The agent needs more information';
  $('input-description').textContent=approval?'Review the commands, resource targets, and effects above. Accept to execute this stage.':pending.text;
  $('reply').hidden=approval; $('reply-label').hidden=approval; $('reply').type=secret?'password':'text';
  $('reply').autocomplete=secret?'new-password':'off'; $('reply').required=!approval;
  $('reply-label').textContent=secret?'Hidden value':'Your answer';
  $('submit-reply').textContent=approval?'Accept plan':secret?'Submit hidden value':'Send answer';
  $('decline').textContent=approval?'Decline':'Cancel task';
  $('input-panel').scrollIntoView({behavior:'smooth',block:'nearest'});
  if (!approval) $('reply').focus({preventScroll:true});
}
async function pollTask() {
  const id=selected; if(!id) return;
  try {
    const data=await api(`/api/jobs/${id}?after=${cursor}`); if(selected!==id) return;
    const job=data.job; $('task-title').textContent=job.request;
    renderUsage(job.llm_usage);
    renderTaskScope(job);
    $('task-status').textContent=statusName(job.status); $('task-status').className='status '+job.status;
    $('task-summary').textContent=job.summary; $('cancel-task').hidden=finals.has(job.status);
    const area=$('activity'); const nearBottom=area.scrollHeight-area.scrollTop-area.clientHeight<70;
    for(const event of data.events) {
      cursor=Math.max(cursor,event.cursor);
      if(event.kind==='command_result') { area.append(renderCommand(event)); continue; }
      if(event.kind!=='output' && event.kind!=='input_received') continue;
      const line=document.createElement('div'); line.className='event'+(event.kind==='input_received'?' note':/^\s*(ACTION:|DISCOVERY:|VERIFICATION:|Planning stage)/.test(event.text)?' phase':'');
      line.textContent=event.text; area.append(line);
    }
    renderPrompt(job.pending);
    if(nearBottom) area.scrollTop=area.scrollHeight;
    $('connection-state').textContent='Service connected';
    // Drain paginated events even after a task finishes.
    if(!finals.has(job.status) || data.events.length===250) pollTimer=setTimeout(pollTask,1000);
    refreshHistory().catch(()=>{});
  } catch(error) {
    if(selected!==id) return;
    $('connection-state').textContent='Reconnecting'; showError(error.message); pollTimer=setTimeout(pollTask,3000);
  }
}
async function runTask() {
  if($('run-task').disabled) return;
  const request=$('request').value.trim(); if(!request) {$('request').focus();return;}
  $('run-task').disabled=true; showError('');
  try {
    const job=await api('/api/jobs',{method:'POST',body:JSON.stringify({request,cloud:$('cloud').value,profile:$('profile').value.trim(),region:$('region').value.trim(),scope:$('compartment').value.trim(),plan_only:$('plan-only').checked})});
    $('request').value=''; await selectTask(job.id);
  } catch(error) { showError(error.message); }
  finally { $('run-task').disabled=false; }
}
async function submitReply(event) {
  event.preventDefault(); if(!activePrompt || !selected) return;
  const prompt=activePrompt, id=selected, answer=prompt.kind==='approval'?'ACCEPT':$('reply').value;
  $('reply').value=''; $('submit-reply').disabled=true; showError('');
  try { await api(`/api/jobs/${id}/respond`,{method:'POST',body:JSON.stringify({prompt_id:prompt.id,answer})}); activePrompt=null; $('input-panel').hidden=true; }
  catch(error) { showError(error.message); }
  finally { $('submit-reply').disabled=false; }
}
async function cancelTask() {
  if(!selected) return;
  try { await api(`/api/jobs/${selected}/cancel`,{method:'POST',body:'{}'}); $('reply').value=''; }
  catch(error) {showError(error.message);}
}
$('run-task').onclick=runTask; $('new-task').onclick=resetTask; $('cancel-task').onclick=cancelTask;
$('profile').onchange=updateCompartmentName; $('region').onchange=updateCompartmentName;
$('compartment').onchange=updateCompartmentName;
$('compartment').oninput=()=>{clearTimeout(compartmentTimer);compartmentTimer=setTimeout(updateCompartmentName,500);};
$('request').onkeydown=event=>{if(event.ctrlKey && event.key==='Enter'){event.preventDefault();runTask();}};
$('reply-form').onsubmit=submitReply;
$('decline').onclick=async()=>{
  if(activePrompt?.kind==='approval') {
    try { await api(`/api/jobs/${selected}/respond`,{method:'POST',body:JSON.stringify({prompt_id:activePrompt.id,answer:'DECLINE'})}); activePrompt=null; $('input-panel').hidden=true; }
    catch(error){showError(error.message);}
  } else cancelTask();
};
document.querySelectorAll('[data-example]').forEach(button=>button.onclick=()=>{$('request').value=button.dataset.example;$('request').focus();});
async function boot() {
  try {
    config=await api('/api/bootstrap'); csrf=config.csrf;
    for(const [id,provider] of Object.entries(config.providers)){const option=document.createElement('option');option.value=id;option.textContent=provider.label;$('cloud').append(option);}
    $('cloud').value='oci'; $('cloud').onchange=changeCloud; changeCloud();
    $('model').textContent=`OCI-hosted planner: ${config.model} · ${config.model_region}`;
    $('version').textContent=`v${config.version}`;
    $('connection-state').textContent='Service connected'; await refreshHistory();
    const latest=await api('/api/jobs'); if(latest.jobs.length) await selectTask(latest.jobs[0].id);
  } catch(error) {showError(error.message);$('connection-state').textContent='Service unavailable';}
}
boot();
setInterval(()=>{if(config && !selected) refreshHistory().catch(()=>{});},3000);
