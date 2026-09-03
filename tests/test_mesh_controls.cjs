// Unit tests of the controller with a fake DOM/transport; no browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const directory = path.resolve(__dirname, '../src/arepo_camera_lab');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', checked: false, style: {}, textContent: '', children: [],
    querySelector: () => ({}), addEventListener() {}, setAttribute(k, v) { this[k] = v; },
    replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); },
    async decode() {},
  });
  return elements.get(id);
}
element('meshDensityFloor').value = '100';
element('volumeProfile').value = 'disk';
element('volumeDensityReference').value = '10000';
element('volumeOpacityLength').value = '10000';
element('volumeDensityPower').value = '0.5';
// Exercise the explicit continuous option at Standard quality; a fresh viewer
// starts with fast linear reconstruction and four antialiased rays per pixel.
element('volumeQuality').value = '1';
element('volumeReconstruction').value = 'continuous_linear';
element('volumeFloorSoftening').value = '1';
element('volumeDenseFadeStart').value = '0';
element('volumeDenseOpacityFraction').value = '1';
element('volumePhysicalTransfer').checked = element('volumeClampColorRange').checked = true;
element('showAnnotations').checked = element('meshLighting').checked = true;
element('measureUnit').value = 'auto';
const storage = new Map();
const context = vm.createContext({
  assert, console, Math, JSON, Number, String, Boolean, Array, Promise,
  location: {protocol: 'http:'}, setInterval: () => 1, clearInterval() {},
  window: {addEventListener() {}},
  document: {getElementById: element, createElementNS: () => ({setAttribute(k,v) {this[k]=v;}})},
  localStorage: {getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v)},
  canvas: {width: 1200, height: 800, style: {}, onpointerdown() {}, ondblclick() {}, onpointerup() {},
    getBoundingClientRect: () => ({left: 100, top: 0, width: 600, height: 400}), hasPointerCapture: () => false},
});
vm.runInContext(`
const DATA={scene:{native_mesh_available:true,native_volume_available:true,sha256:'abc',snapshot:31,snapshot_time_seconds:155.0146484375,display_radius_cm:10,center_cm:[100,200,300]},channels:{density:{label:'density',units:'g cm^-3'}}};
const channel={value:'density'},scaleMode={value:'log10'},palette={value:'copper_blue'},gamma={value:'1'},saturation={value:'1'},brightness={value:'1'},invert={checked:false},opacity={value:'.72'},rangeState={linthresh:1};
for(const control of [opacity,gamma,saturation,brightness])control.addEventListener=()=>{};
const currentChannel=DATA.channels.density,paletteGradients={grayscale:'#000,#fff'};
const derivedDefinitions=[];let dragging=false,requests=0,badHash=false,picks=0,holdResponse=false,releaseResponse=null;
function safeRange(){return [1,10];}
function cleanBasis(forward,up){return {forward,up,right:[1,0,0]};}
let camera={target:[0,0,0],scale:2,...cleanBasis([0,0,-1],[0,1,0])};
function setCamera(value){camera={...value,...cleanBasis(value.forward,value.up)};}
function markStyleDirty(){} function markCameraModified(){}
function cloned(value){return JSON.parse(JSON.stringify(value));}
async function fetch(url,options){
  const params=JSON.parse(options.body);
  if(url.endsWith('/pick')){picks++;return {ok:true,json:async()=>({pick:{hit:true,position_cm:picks===1?[100,200,300]:[103,204,312],particle_id:String(picks)}})};}
  requests++;
  if(holdResponse)await new Promise(resolve=>{releaseResponse=resolve;});
  return {ok:true,json:async()=>({png:'dummy',report:{scene_sha256:badHash?'wrong':DATA.scene.sha256,selected_cells:8,native_cell_count:10,faces:12,render_seconds:.05,camera:params.camera,style:params.style,volume:params.volume,representation:params.representation,subpixel_samples:params.subpixel_samples,width:params.width,height:params.height}})};
}
`, context);
vm.runInContext(fs.readFileSync(path.join(directory, 'measurement_math.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(directory, 'mesh_viewer.js'), 'utf8'), context);
(async () => {
  await vm.runInContext(`(async()=>{
    assert.equal(renderMode.value,'volume');
    assert.equal(meshParameters().representation,'volume');
    assert.equal(meshParameters().subpixel_samples,1);
    assert.equal(meshParameters().cell_samples,2);
    assert.equal(meshParameters().volume.reconstruction,'continuous_linear');
    assert.equal(meshParameters().volume.transfer_stage,'after_reconstruction');
    assert.equal(meshParameters().volume.range_behavior,'clamp');
    assert.equal(meshParameters().volume.opacity_length_cm,1e9);
    assert.equal(meshEdges.disabled,true);
    renderMode.value='mesh';renderMode.onchange();
    await requestNativeFrame(); updateMeasurements();
    assert.equal(requests,1);
    assert.match(timeLabel.textContent,/155.015 s/);
    assert.equal(measurementHud.style.display,'block');
    assert.equal(meshParameters().density_floor,100);
    assert.equal(meshParameters().width/meshParameters().height,1.5);
    assert(!legendColor(.5,{...meshStyle(),palette:'grayscale'}).includes('NaN'));
    await requestNativeFrame(); assert.equal(requests,1);
    camera.scale=1; badHash=true;
    const previous=meshLastReport;
    await requestNativeFrame(); assert.equal(meshLastReport,previous);
    assert.match(meshNotice.textContent,/different snapshot/);
    await requestNativeFrame(); assert.equal(requests,2);
    badHash=false; await requestNativeFrame(true); assert.equal(requests,3);
    await rulerPick({clientX:300,clientY:200});
    await rulerPick({clientX:400,clientY:200});
    assert.equal(window.cameraLabMeasurements().distance_cm,13);
    assert.equal(window.cameraLabMeasurements().kind,'3d');
    assert.match(rulerStatus.textContent,/3D/);
    const saved=meshViewState(); meshDensityFloor.value='5';applyMeshViewState(saved);
    assert.equal(meshDensityFloor.value,'100');
    renderMode.value='points';renderMode.onchange();
    await rulerPick({clientX:250,clientY:200});
    await rulerPick({clientX:550,clientY:200});
    assert.equal(window.cameraLabMeasurements().kind,'projected');
    assert.equal(window.cameraLabMeasurements().distance_cm,15);
    renderMode.value='volume';renderMode.onchange();
    await requestNativeFrame();
    const comparedCamera=JSON.stringify(camera),comparedStyle=JSON.stringify(meshStyle());
    const comparisonRequests=requests,comparedReport=meshLastReport;
    comparePoints.onclick();
    assert.equal(renderMode.value,'points');
    assert.equal(meshImage.style.display,'none');
    assert.equal(meshLastReport,null);
    assert.match(comparePoints.textContent,/Return to native volume/);
    comparePoints.onclick();
    assert.equal(renderMode.value,'volume');
    assert.equal(meshLastReport,comparedReport);
    assert.equal(meshImage.style.display,'block');
    assert.equal(JSON.stringify(camera),comparedCamera);
    assert.equal(JSON.stringify(meshStyle()),comparedStyle);
    await requestNativeFrame();assert.equal(requests,comparisonRequests);
    // Changed opacity must render again rather than display the cached frame.
    comparePoints.onclick();opacity.value='.4';comparePoints.onclick();
    assert.equal(meshLastReport,null);assert.equal(meshImage.style.display,'none');
    await requestNativeFrame();assert.equal(requests,comparisonRequests+1);
    opacity.value='.72';await requestNativeFrame();
    // A changed snapshot or an image still decoding cannot inherit old metadata.
    comparePoints.onclick();DATA.scene.sha256='another-scene';comparePoints.onclick();
    assert.equal(meshLastReport,null);assert.equal(meshImage.style.display,'none');
    DATA.scene.sha256='abc';await requestNativeFrame();
    comparePoints.onclick();meshImage.src='pending-image';comparePoints.onclick();
    assert.equal(meshLastReport,null);assert.equal(meshImage.style.display,'none');
    await requestNativeFrame();
    await rulerPick({clientX:250,clientY:200});
    await rulerPick({clientX:550,clientY:200});
    assert.equal(picks,2); // Volume never reuses the face picker or invents a surface.
    assert.equal(window.cameraLabMeasurements().kind,'projected');
    assert.equal(window.cameraLabMeasurements().distance_cm,15);
    assert.equal(window.cameraLabMeasurements().projection.camera.scale,1);
    volumeProfile.value='outflow';volumeProfile.onchange();
    assert.equal(meshParameters().density_floor,.01);
    assert.equal(meshParameters().subpixel_samples,1);
    assert.equal(meshParameters().width,480);
    assert.equal(meshParameters().volume.reconstruction,'linear');
    assert.equal(meshParameters().volume.transfer_stage,'after_reconstruction');
    volumeQuality.value='4';
    nativeInteractiveUntil=0;
    assert.equal(meshParameters().subpixel_samples,4);
    assert.equal(meshParameters().volume.reconstruction,'continuous_linear');
    // A low-resolution fit completing after interaction ends must still refine.
    meshFitRequested=true;noteNativeInteraction();holdResponse=true;
    const beforeFit=requests,fitPending=requestNativeFrame(true);
    nativeInteractiveUntil=0;releaseResponse();await fitPending;holdResponse=false;
    assert.equal(meshLastReport.width,480);
    assert.equal(meshLastReport.volume.reconstruction,'linear');
    assert.notEqual(meshLastKey,meshKey(meshParameters()));
    await requestNativeFrame();
    assert.equal(requests,beforeFit+2);
    assert.equal(meshLastReport.width,canvas.width);
    assert.equal(meshLastReport.subpixel_samples,4);
    assert.equal(meshLastReport.volume.reconstruction,'continuous_linear');
    volumeFloorSoftening.value='0.3';
    volumeDenseFadeStart.value='10000';volumeDenseOpacityFraction.value='0.08';
    const volumeSaved=meshViewState();volumeDensityReference.value='1e9';applyMeshViewState(volumeSaved);
    assert.equal(volumeDensityReference.value,'10000');
    assert.equal(volumeProfile.value,'outflow');
    assert.equal(volumeFloorSoftening.value,'0.3');
    assert.equal(volumeReconstruction.value,'continuous_linear');
    assert.equal(volumeDenseFadeStart.value,'10000');
    assert.equal(volumeDenseOpacityFraction.value,'0.08');
    assert.equal(volumePhysicalTransfer.checked,true);
    assert.equal(volumeClampColorRange.checked,true);
    delete volumeSaved.volume.reconstruction;applyMeshViewState(volumeSaved);
    assert.equal(volumeReconstruction.value,'piecewise_constant');
    for(const name of ['transfer_stage','range_behavior','dense_fade_start','dense_opacity_fraction'])delete volumeSaved.volume[name];
    applyMeshViewState(volumeSaved);
    assert.equal(volumePhysicalTransfer.checked,false);
    assert.equal(volumeClampColorRange.checked,false);
    assert.equal(volumeDenseFadeStart.value,'0');
    assert.equal(volumeDenseOpacityFraction.value,'1');
    holdResponse=true;
    const pending=requestNativeFrame(true);
    renderMode.value='points';renderMode.onchange();
    releaseResponse();await pending;holdResponse=false;
    assert.equal(meshImage.style.display,'none');
    assert.equal(meshLastReport,null);
    showAnnotations.checked=false; updateMeasurements();
    assert.equal(measurementHud.style.display,'none');
  })()`, context);
  console.log('Native volume/face controls, point comparison and cache guards, refinement, presets, time/scale legend, and rulers passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
