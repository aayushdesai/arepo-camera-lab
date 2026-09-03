// Exercise the real shell script using an in-memory iframe and transport.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map();
function element(id){
  if(!elements.has(id))elements.set(id,{value:'',checked:false,style:{},options:[],textContent:'',
    classList:{add(){},remove(){}},listeners:{},addEventListener(name,fn){this.listeners[name]=fn;},
    replaceChildren(){this.options=[];},appendChild(item){this.options.push(item);}});
  return elements.get(id);
}
element('keepView').checked=true;element('points').value='400000';
const context=vm.createContext({assert,console,window:{close(){}},
  document:{getElementById:element,createElement:()=>({dataset:{}})},
  sessionStorage:{setItem(){},getItem(){return null;}},setInterval:()=>1,clearInterval(){}});
vm.runInContext(`
let responseData={loaded:false,loading:false,progress:0},rejectLoad=false,readCount=0,restoreCount=0,restored=null;
let currentInspection={schema:'fixture',opacity:.7};
const previousViewer={cameraLabInspectionState(){readCount++;return {...currentInspection};}};
document.getElementById('viewer').contentWindow=previousViewer;
async function fetch(url){return {ok:!(url==='/api/load'&&rejectLoad),json:async()=>url==='/api/catalog'?{frames:[{snapshot:31,label:'fixture'}]}:url==='/api/load'?{error:rejectLoad?'fixture load failure':null}:responseData};}
function loaded(snapshot,revision){return {loaded:true,loading:false,revision,snapshot,scene_sha256:'scene-'+snapshot,scene_path:'/fixture/scene',point_count:10,num_cells:100,requested_points:400000,progress:1};}
`,context);
const source=fs.readFileSync(0,'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
vm.runInContext(source,context);
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  await vm.runInContext(`(async()=>{
    snapshot.value='721';await load.onclick();
    currentInspection.opacity=.25;responseData=loaded(721,1);await refresh();
    assert.equal(readCount,1);assert.equal(pendingInspection.state.opacity,.25);
    assert.match(frame.src,/inspection=1/);
    frame.contentWindow={AREPO_CAMERA_LAB_CAPTURE:{scene:{snapshot:31,sha256:'scene-31'}},cameraLabRestoreInspection(state,expected){restoreCount++;restored={state,expected};}};
    frame.listeners.load();assert.equal(restoreCount,0);assert(pendingInspection);
    frame.contentWindow.AREPO_CAMERA_LAB_CAPTURE.scene={snapshot:721,sha256:'scene-721'};
    frame.listeners.load();assert.equal(restoreCount,1);assert.equal(restored.expected.sha256,'scene-721');
    assert.equal(restored.state.opacity,.25);assert.equal(pendingInspection,null);
    assert.match(inspectionNotice,/preserved/);
    // The option can be disabled and explicit pose navigation remains separate.
    keepView.checked=false;snapshot.value='31';await load.onclick();
    responseData=loaded(31,2);await refresh();
    assert(!frame.src.includes('inspection=1'));assert.equal(readCount,1);
    keepView.checked=true;responseData=loaded(721,3);await refresh();
    assert(!frame.src.includes('inspection=1'));assert.equal(pendingInspection,null);
    // Both immediate and background load failures discard the carry request.
    rejectLoad=true;snapshot.value='31';await load.onclick();assert.equal(keepViewTarget,null);
    rejectLoad=false;await load.onclick();responseData={...loaded(721,3),error:'cache unavailable'};await refresh();
    assert.equal(keepViewTarget,null);assert.equal(pendingInspection,null);
    // A restore error stays visible and does not get retried against another scene.
    frame.contentWindow=previousViewer;snapshot.value='31';await load.onclick();
    responseData=loaded(31,4);await refresh();
    frame.contentWindow={AREPO_CAMERA_LAB_CAPTURE:{scene:{snapshot:31,sha256:'scene-31'}},cameraLabRestoreInspection(){throw new Error('field unavailable');}};
    frame.listeners.load();assert.equal(pendingInspection,null);assert.match(inspectionNotice,/field unavailable/);
  })()`,context);
  console.log('Snapshot shell preserves the latest view only for requested loads and rejects stale frames and failed loads');
})().catch(error=>{console.error(error);process.exitCode=1;});
