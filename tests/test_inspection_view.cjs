// Physical viewport transfer across synthetic snapshots; no browser or network.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const context=vm.createContext({assert,console,window:{}});
vm.runInContext(`
const DATA={scene:{snapshot:1,sha256:'scene-one',center_cm:[100,200,300],display_radius_cm:10,snapshot_time_seconds:5},channels:{density:{units:'g cm^-3'}}};
const canvas={width:1200,height:800},renderMode={value:'volume'},meshImage={style:{display:'block'}};
let camera={target:[1,2,3],forward:[0,0,-1],up:[0,1,0],scale:2};
let visual={channel:'density',low:100,high:1e6,opacity:.4,mesh_view:{volume:{reconstruction:'linear',transfer_stage:'after_reconstruction',dense_fade_start:10000,dense_opacity_fraction:.08}}};
let applied=0,cameraChanged=0,styleChanged=0,measured=0,meshFitRequested=true,nativeInteractiveUntil=10,meshFailureKey='failed',meshLastKey='old',meshLastReport={},meshDisplayedCamera={};
function cloned(x){return JSON.parse(JSON.stringify(x));}
function pose(){return {snapshot:DATA.scene.snapshot,scene_sha256:DATA.scene.sha256,look_at_cm:camera.target.map((x,i)=>DATA.scene.center_cm[i]+x*DATA.scene.display_radius_cm),view_direction:[...camera.forward],up:[...camera.up],screen_half_extent_cm:camera.scale*DATA.scene.display_radius_cm};}
function visualState(){return cloned(visual);}
function resize(){} function applyVisualState(x){applied++;visual=cloned(x);}
function setCamera(x){camera=cloned(x);} function markCameraModified(){cameraChanged++;}
function markStyleDirty(){styleChanged++;} function updateMeasurements(){measured++;}
`,context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/arepo_camera_lab/inspection_view.js'),'utf8'),context);
vm.runInContext(`
const saved=window.cameraLabInspectionState();
DATA.scene={snapshot:2,sha256:'scene-two',center_cm:[90,190,290],display_radius_cm:20,snapshot_time_seconds:10};
canvas.width=900;canvas.height=600;
const expected={snapshot:2,sha256:'scene-two'};
const report=window.cameraLabRestoreInspection(saved,expected);
assert.equal(report.scene_sha256,'scene-two');assert.equal(report.snapshot,2);
assert.deepEqual(camera.target,[1,1.5,2]);assert.equal(camera.scale,1);
assert.deepEqual(pose().look_at_cm,saved.physical.look_at_cm);
assert.equal(pose().screen_half_extent_cm,saved.physical.screen_half_extent_cm);
assert.equal(DATA.scene.snapshot_time_seconds,10);
assert.equal(visual.low,100);assert.equal(visual.high,1e6);assert.equal(visual.opacity,.4);
assert.deepEqual(visual.mesh_view,saved.visual.mesh_view);
assert.deepEqual(visual.canvas_size,{width:900,height:600});
assert.equal(meshFitRequested,false);assert.equal(meshLastReport,null);
assert.equal(meshImage.style.display,'none');assert.equal(cameraChanged,1);assert.equal(styleChanged,1);
const before=JSON.stringify(camera);
assert.throws(()=>window.cameraLabRestoreInspection(saved,{snapshot:1,sha256:'scene-one'}),/not displayed/);
assert.throws(()=>window.cameraLabRestoreInspection({...saved,channel_units:'different'},expected),/units changed/);
assert.throws(()=>window.cameraLabRestoreInspection({...saved,visual:{...saved.visual,channel:'missing'}},expected),/unavailable/);
assert.throws(()=>window.cameraLabRestoreInspection({...saved,physical:{...saved.physical,screen_half_extent_cm:NaN}},expected),/camera is invalid/);
assert.equal(applied,1);assert.equal(JSON.stringify(camera),before);
`,context);
console.log('Snapshot inspection preserves physical camera, field range, transparency, units, and new-snapshot binding');
