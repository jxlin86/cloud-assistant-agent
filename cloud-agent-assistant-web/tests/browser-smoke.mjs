// Drive an installed Chromium/Edge through CDP; no extra browser package needed.
import {writeFile} from 'node:fs/promises';
const [url='http://127.0.0.1:8767',scenario='workflow',screenshot=''] = process.argv.slice(2);
const tabs=await (await fetch(`http://127.0.0.1:${process.env.BROWSER_DEBUG_PORT || 9224}/json/list`)).json();
const tab=tabs.find(tab=>tab.type==='page');
if(!tab) throw Error('No browser page');
const socket=new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
let seq=0;const pending=new Map();const errors=[];
socket.onmessage=event=>{
  const message=JSON.parse(event.data);
  if(message.id && pending.has(message.id)) {const item=pending.get(message.id);pending.delete(message.id);clearTimeout(item.timer);message.error?item.reject(Error(JSON.stringify(message.error))):item.resolve(message.result);}
  if(message.method==='Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.text);
};
function send(method,params={}) {
  return new Promise((resolve,reject)=>{const id=++seq;const timer=setTimeout(()=>{pending.delete(id);reject(Error(method+' timed out'));},15000);pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));});
}
async function evaluate(expression) {
  const value=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
  if(value.exceptionDetails) throw Error(JSON.stringify(value.exceptionDetails));
  return value.result.value;
}
async function until(expression) {
  for(let attempt=0;attempt<80;attempt++){if(await evaluate(expression)) return;await new Promise(resolve=>setTimeout(resolve,100));}
  throw Error('UI condition failed: '+expression+'; body: '+await evaluate('document.body.textContent.slice(-2500)'));
}
try {
  await send('Runtime.enable');await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1050,deviceScaleFactor:1,mobile:false});
  await send('Page.navigate',{url});
  await until("document.getElementById('connection-state')?.textContent==='Service connected'");
  const state=await evaluate("({profile:document.getElementById('profile').value,errors:document.getElementById('error').textContent,overflow:document.documentElement.scrollWidth>innerWidth})");
  if(state.profile!=='OCIPRJ' || state.errors || state.overflow) throw Error(JSON.stringify(state));
  if(scenario==='workflow') {
    await until("document.getElementById('compartment-name').textContent==='Name: fde-sandbox'");
    if(!await evaluate("document.getElementById('compartment').value==='ocid1.compartment.test'")) throw Error('Compartment OCID was replaced by its name');
    await evaluate("document.getElementById('request').value='List simulated projects';document.getElementById('run-task').click()");
    await until("document.getElementById('task-title').textContent==='List simulated projects' && !document.getElementById('run-task').disabled");
    await until("document.getElementById('task-status').textContent==='succeeded'");
    if(!await evaluate("document.getElementById('task-scope').textContent.includes('fde-sandbox (ocid1.compartment.test)' )")) throw Error('Missing compartment name and OCID in task details');
    if(!await evaluate("document.getElementById('activity').textContent.includes('FDE-Service')")) throw Error('Missing readable result');
    if(!await evaluate("document.getElementById('input-tokens').textContent==='240' && document.getElementById('output-tokens').textContent==='50' && document.getElementById('total-tokens').textContent==='290' && document.getElementById('llm-calls').textContent==='2'")) throw Error('Missing task token metrics');
    await evaluate("renderUsage(undefined)");
    if(!await evaluate("document.getElementById('input-tokens').textContent==='—' && document.getElementById('token-usage-note').textContent.includes('No usage recorded')")) throw Error('Old history incorrectly reports zero usage');
    await evaluate("renderUsage({input_tokens:0,output_tokens:0,total_tokens:0,requests:1,missing_usage_requests:1})");
    if(!await evaluate("document.getElementById('input-tokens').textContent==='—' && document.getElementById('token-usage-note').textContent.includes('without token counts')")) throw Error('Missing provider usage incorrectly reports zero');
    if(!await evaluate("document.querySelector('.command-card .command-line')?.textContent.includes('devops project get') && document.querySelector('.command-output')?.textContent.includes('FDE-Service')")) throw Error('Missing command or readable output');
    await evaluate("document.querySelector('.command-json summary').click()");
    if(!await evaluate("document.querySelector('.command-json').open && JSON.parse(document.querySelector('.command-json pre').textContent).response.data['display-name']==='FDE-Service'")) throw Error('Missing JSON output');
    await evaluate("document.getElementById('request').value='Create a simulated project';document.getElementById('run-task').click()");
    await until("document.getElementById('task-title').textContent==='Create a simulated project' && !document.getElementById('run-task').disabled");
    await until("document.getElementById('task-status').textContent==='awaiting approval'");
    if(!await evaluate("!document.getElementById('input-panel').hidden && document.getElementById('submit-reply').textContent==='Accept plan'")) throw Error('Approval not displayed');
    await evaluate("document.getElementById('submit-reply').click()");
    await until("document.getElementById('task-status').textContent==='succeeded'");
    await evaluate("document.getElementById('request').value='Create simulated resource using secret input';document.getElementById('run-task').click()");
    await until("document.getElementById('task-title').textContent==='Create simulated resource using secret input' && !document.getElementById('run-task').disabled");
    await until("document.getElementById('task-status').textContent==='awaiting approval'");
    await evaluate("document.getElementById('submit-reply').click()");
    await until("document.getElementById('task-status').textContent==='awaiting secret'");
    if(!await evaluate("document.getElementById('reply').type==='password'")) throw Error('Secret not hidden');
    await evaluate("document.getElementById('reply').value='FAKE-browser-test-secret-123';document.getElementById('submit-reply').click()");
    await until("document.getElementById('task-status').textContent==='succeeded'");
    if(await evaluate("document.body.textContent.includes('FAKE-browser-test-secret-123') || document.getElementById('reply').value!==''")) throw Error('Secret leaked into DOM');
    await send('Page.reload');
    await until("document.getElementById('task-status')?.textContent==='succeeded'");
    if(!await evaluate("document.querySelectorAll('.command-card').length===2 && document.querySelector('.command-json pre').textContent.includes('[REDACTED]')")) throw Error('Command history was not restored safely');
    if(!await evaluate("Number(document.getElementById('input-tokens').textContent)>0 && Number(document.getElementById('output-tokens').textContent)>0")) throw Error('Token metrics not restored after reload');
    if(!await evaluate("document.getElementById('task-scope').textContent.includes('fde-sandbox (ocid1.compartment.test)' )")) throw Error('Compartment name and OCID not restored after reload');
    console.log('PASS: browser read-only results, write approval, hidden input, and history reload (simulated OCI only).');
  }
  if(scenario==='multicloud') {
    if(!await evaluate("document.getElementById('cloud').options.length===4")) throw Error('Missing provider selector');
    const contexts={azure:{scope:'11111111-2222-3333-4444-555555555555',region:'eastus',label:'Azure subscription ID'},gcp:{scope:'cloud-test-project',region:'us-central1',label:'Google Cloud project ID'},aws:{scope:'123456789012',region:'us-east-1',label:'AWS account ID (12 digits)'}};
    for(const [cloud,context] of Object.entries(contexts)) {
      await evaluate(`document.getElementById('cloud').value=${JSON.stringify(cloud)};document.getElementById('cloud').dispatchEvent(new Event('change'))`);
      if(!await evaluate(`document.getElementById('scope-label').textContent===${JSON.stringify(context.label)} && document.getElementById('region').value===${JSON.stringify(context.region)}`)) throw Error('Cloud context form did not update');
      await evaluate(`document.getElementById('compartment').value=${JSON.stringify(context.scope)};document.getElementById('request').value=${JSON.stringify('List simulated '+cloud+' resources')};document.getElementById('run-task').click()`);
      await until(`document.getElementById('task-title').textContent===${JSON.stringify('List simulated '+cloud+' resources')} && !document.getElementById('run-task').disabled`);
      await until("document.getElementById('task-status').textContent==='succeeded'");
      if(!await evaluate(`document.getElementById('task-scope').textContent.includes(${JSON.stringify(context.scope)}) && document.getElementById('input-tokens').textContent==='200' && document.querySelector('.command-card')`)) throw Error('Missing cloud results, scope, or metrics');
    }
    await evaluate("document.getElementById('request').value='Create simulated aws resource';document.getElementById('run-task').click()");
    await until("document.getElementById('task-title').textContent==='Create simulated aws resource' && !document.getElementById('run-task').disabled");
    await until("document.getElementById('task-status').textContent==='awaiting approval'");
    await evaluate("document.getElementById('submit-reply').click()");
    await until("document.getElementById('task-status').textContent==='succeeded'");
    await send('Page.reload');
    await until("document.getElementById('task-title')?.textContent==='Create simulated aws resource' && document.getElementById('task-status').textContent==='succeeded'");
    if(!await evaluate("document.getElementById('task-scope').textContent.includes('AWS') && document.getElementById('input-tokens').textContent==='300'")) throw Error('Provider or usage lost on reload');
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    if(await evaluate('document.documentElement.scrollWidth>innerWidth')) throw Error('Mobile page overflows');
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1050,deviceScaleFactor:1,mobile:false});
    await evaluate("document.getElementById('cloud').value='aws';document.getElementById('cloud').dispatchEvent(new Event('change'));document.getElementById('compartment').value='123456789012'");
    console.log('PASS: Azure, Google Cloud and AWS provider selection, scoped reads, write approval, usage, history reload and mobile layout (all cloud calls simulated).');
  }
  if(errors.length) throw Error('Browser errors: '+errors.join(', '));
  if(screenshot) {const png=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});await writeFile(screenshot,Buffer.from(png.data,'base64'));}
  console.log('PASS: rendered browser page; no JavaScript errors or horizontal overflow.');
} finally {socket.close();}
