const renderMode=document.getElementById('renderMode'),meshImage=document.getElementById('meshImage'),meshNotice=document.getElementById('meshNotice');
const comparePoints=document.getElementById('comparePoints');
const meshEdges=document.getElementById('meshEdges'),meshInterior=document.getElementById('meshInterior'),meshLighting=document.getElementById('meshLighting');
const meshDensityFloor=document.getElementById('meshDensityFloor'),meshFit=document.getElementById('meshFit');
const cellControls=document.getElementById('cellControls'),cellDensityControls=document.getElementById('cellDensityControls'),meshOpacityMode=document.getElementById('meshOpacityMode'),meshZoomOpacity=document.getElementById('meshZoomOpacity');
const meshInteriorControl=document.getElementById('meshInteriorControl');
const reconstructionControls=document.getElementById('reconstructionControls'),physicalTransferControl=document.getElementById('physicalTransferControl');
const volumeControls=document.getElementById('volumeControls'),volumeProfile=document.getElementById('volumeProfile'),volumeDensityReference=document.getElementById('volumeDensityReference'),volumeOpacityLength=document.getElementById('volumeOpacityLength'),volumeDensityPower=document.getElementById('volumeDensityPower'),volumeQuality=document.getElementById('volumeQuality');
const volumeReconstruction=document.getElementById('volumeReconstruction'),volumeFloorSoftening=document.getElementById('volumeFloorSoftening');
const volumePhysicalTransfer=document.getElementById('volumePhysicalTransfer'),volumeClampColorRange=document.getElementById('volumeClampColorRange'),volumeDenseFadeStart=document.getElementById('volumeDenseFadeStart'),volumeDenseOpacityFraction=document.getElementById('volumeDenseOpacityFraction');
const measurementHud=document.getElementById('measurementHud'),timeLabel=document.getElementById('timeLabel'),scaleLabel=document.getElementById('scaleLabel'),scaleLine=document.getElementById('scaleLine');
const legendTitle=document.getElementById('legendTitle'),legendGradient=document.getElementById('legendGradient'),legendLow=document.getElementById('legendLow'),legendHigh=document.getElementById('legendHigh');
const rulerToggle=document.getElementById('rulerToggle'),rulerStatus=document.getElementById('rulerStatus'),rulerOverlay=document.getElementById('rulerOverlay'),showAnnotations=document.getElementById('showAnnotations'),measureUnit=document.getElementById('measureUnit');
let meshBusy=false,meshPromise=null,meshLastKey=null,meshDisplayedCamera=null,meshLastReport=null,meshRequestSequence=0,meshFailureKey=null,meshFitRequested=false;
let meshCachedFrame=null;
let rulerPoints=[],rulerKind=null,rulerPicking=false,rulerProjection=null;
let nativeInteractiveUntil=0;
let meshZoomReferenceHalfCm=null;
const meshLive=Boolean(DATA.scene.native_mesh_available)&&location.protocol.startsWith('http');
const volumeLive=meshLive&&Boolean(DATA.scene.native_volume_available);
const savedRenderer=localStorage.getItem('arepo_camera_lab_renderer_v002')||localStorage.getItem('arepo_camera_lab_renderer_v001');
renderMode.value=meshLive?(savedRenderer==='points'?'points':savedRenderer==='mesh'?'mesh':volumeLive?'volume':'mesh'):'points';
let compareReturnMode=renderMode.value==='mesh'?'mesh':volumeLive?'volume':'mesh';
if(!meshLive)renderMode.querySelector('[value="mesh"]').disabled=true;
if(!volumeLive)renderMode.querySelector('[value="volume"]').disabled=true;
if(!volumeLive){meshOpacityMode.value='uniform';meshOpacityMode.querySelector('[value="density"]').disabled=true;}
meshNotice.textContent=meshLive?'Native cell volume and geometry views; choose a transparency profile to expose the structure.':'Native views need the live server and a scene with native connectivity.';
function nativeMode(){return renderMode.value==='mesh'||renderMode.value==='volume';}
function densityCells(){return renderMode.value==='mesh'&&volumeLive&&meshOpacityMode.value==='density';}
function surfaceCells(){return renderMode.value==='mesh'&&!densityCells();}
function noteNativeInteraction(){nativeInteractiveUntil=Date.now()+160;}
function syncNativeControls(){
  const volume=renderMode.value==='volume',density=densityCells();volumeControls.style.display=volume||density?'block':'none';
  cellControls.style.display=renderMode.value==='mesh'?'block':'none';
  cellDensityControls.style.display=density?'block':'none';
  reconstructionControls.style.display=physicalTransferControl.style.display=volume?'block':'none';
  meshEdges.disabled=renderMode.value!=='mesh';
  meshInterior.disabled=!surfaceCells();meshLighting.disabled=renderMode.value!=='mesh';
  meshInteriorControl.style.display=density?'none':'block';
  meshDensityFloor.disabled=meshFit.disabled=!nativeMode();
  comparePoints.disabled=!meshLive;
  comparePoints.textContent=nativeMode()?'Compare with point cloud':`Return to native ${compareReturnMode==='volume'?'volume':'faces'}`;
  if(!rulerPoints.length)rulerStatus.textContent=surfaceCells()?'The ruler picks native cell surfaces and measures 3D distance.':'The ruler measures projected distance in the camera plane.';
}
function volumeState(){return {density_reference:+volumeDensityReference.value,opacity_length_cm:+volumeOpacityLength.value*1e5,density_power:+volumeDensityPower.value,floor_softening_dex:+volumeFloorSoftening.value,reconstruction:volumeReconstruction.value,transfer_stage:volumePhysicalTransfer.checked?'after_reconstruction':'before_reconstruction',range_behavior:volumeClampColorRange.checked?'clamp':'hide',dense_fade_start:+volumeDenseFadeStart.value,dense_opacity_fraction:+volumeDenseOpacityFraction.value};}
volumeProfile.onchange=()=>{
  const profile={disk:[100,1e4,.5],remnant:[100,1e6,.7],outflow:[.01,1e4,.5]}[volumeProfile.value];
  if(profile){meshDensityFloor.value=String(profile[0]);volumeDensityReference.value=String(profile[1]);volumeDensityPower.value=String(profile[2]);volumeOpacityLength.value='10000';volumeFloorSoftening.value='1';volumeDenseFadeStart.value='0';volumeDenseOpacityFraction.value='1';noteNativeInteraction();markStyleDirty();}
};
for(const control of [volumeDensityReference,volumeOpacityLength,volumeDensityPower,volumeFloorSoftening,volumeDenseFadeStart,volumeDenseOpacityFraction])control.addEventListener('input',()=>{volumeProfile.value='custom';noteNativeInteraction();});
syncNativeControls();
function meshStyle(){const [low,high]=safeRange();return {channel:channel.value,scale_mode:scaleMode.value,low,high,linthresh:rangeState.linthresh,palette:palette.value,gamma:+gamma.value,saturation:+saturation.value,brightness:+brightness.value,invert:invert.checked,opacity:+opacity.value};}
function meshParameters(){
  const interactive=dragging||Date.now()<nativeInteractiveUntil,limit=interactive?480:1440;
  const factor=Math.min(1,limit/canvas.width,1200/canvas.height),width=Math.max(1,Math.round(canvas.width*factor)),height=Math.max(1,Math.round(canvas.height*factor));
  const volume=volumeState(),density=densityCells(),continuous=!density&&volume.reconstruction==='continuous_linear';
  if(interactive&&continuous)volume.reconstruction='linear';
  if(density){volume.reconstruction='piecewise_constant';volume.transfer_stage='after_reconstruction';}
  if(density&&!(meshZoomReferenceHalfCm>0))meshZoomReferenceHalfCm=camera.scale*DATA.scene.display_radius_cm;
  return {scene_sha256:DATA.scene.sha256,representation:renderMode.value==='volume'?'volume':density?'cells':'faces',camera:{target:[...camera.target],forward:[...camera.forward],up:[...camera.up],scale:camera.scale},style:meshStyle(),derived_channels:derivedDefinitions,edges:meshEdges.checked,interior_faces:meshInterior.checked,lighting:meshLighting.checked,density_floor:+meshDensityFloor.value,volume,subpixel_samples:interactive?1:+volumeQuality.value,cell_samples:density||interactive?1:continuous||+volumeQuality.value===4?2:1,zoom_opacity:density?{enabled:meshZoomOpacity.checked,reference_half_extent_cm:meshZoomReferenceHalfCm}:undefined,fit_visible:meshFitRequested,width,height};
}
function meshKey(params){return JSON.stringify(params);}
async function requestNativeFrame(force=false){
  if(!nativeMode())return;
  const params=meshParameters(),key=meshKey(params),requestedMode=renderMode.value;
  if(meshBusy)return meshPromise;
  if(!force&&(key===meshLastKey||key===meshFailureKey))return;
  meshBusy=true;meshFailureKey=null;const sequence=++meshRequestSequence;
  meshNotice.textContent=meshLastReport?'Updating the native view…':'Loading the native view…';
  meshPromise=(async()=>{
    try{
      const response=await fetch('/api/mesh/render',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params)}),result=await response.json();
      if(!response.ok||!result.png)throw new Error(result.error||`HTTP ${response.status}`);
      if(result.report.scene_sha256!==DATA.scene.sha256)throw new Error('Mesh frame belongs to a different snapshot.');
      if(sequence!==meshRequestSequence||renderMode.value!==requestedMode)return;
      meshImage.src='data:image/png;base64,'+result.png;await meshImage.decode();
      if(sequence!==meshRequestSequence||renderMode.value!==requestedMode)return;
      meshImage.style.display='block';meshLastKey=key;meshDisplayedCamera={...result.report.camera,...cleanBasis(result.report.camera.forward,result.report.camera.up)};meshLastReport=result.report;
      if(params.fit_visible){meshFitRequested=false;setCamera(meshDisplayedCamera);if(result.report.zoom_opacity)meshZoomReferenceHalfCm=result.report.zoom_opacity.reference_half_extent_cm;const current=meshParameters();meshLastKey=meshKey({...params,camera:current.camera,zoom_opacity:current.zoom_opacity,fit_visible:false});}
      const r=result.report,method=r.representation==='cells'?`Original cells · density opacity · ${r.subpixel_samples} rays/pixel`:r.representation==='volume'?`Metal volume · ${{linear:'linear field',continuous_linear:'continuous field',continuous:'legacy smoothing',piecewise_constant:'original cells'}[r.reconstruction]||r.reconstruction} · ${r.subpixel_samples} rays/pixel`:`${r.faces.toLocaleString()} native faces`;meshNotice.textContent=`${r.selected_cells.toLocaleString()} / ${r.native_cell_count.toLocaleString()} selected cells · ${method} · ${r.render_seconds.toFixed(2)} s`;
      if(r.zoom_opacity?.enabled)meshNotice.textContent+=` · foreground opacity ×${Number(r.zoom_opacity.opacity_factor).toPrecision(3)}`;
      if(r.empty_native_faces)meshNotice.textContent+=` · ${r.empty_native_faces} zero-area native faces`;
      meshCachedFrame={key:meshLastKey,src:meshImage.src,report:meshLastReport,camera:meshDisplayedCamera,notice:meshNotice.textContent};
    }catch(error){meshFailureKey=key;meshNotice.textContent='Mesh view: '+error.message;}
    finally{meshBusy=false;}
  })();
  return meshPromise;
}
async function awaitNativeFrame(){
  if(meshPromise)await meshPromise;
  const wanted=meshKey(meshParameters());
  if(meshLastKey!==wanted)await requestNativeFrame(true);
  if(meshLastKey!==wanted)throw new Error(meshNotice.textContent);
}
renderMode.onchange=()=>{
  localStorage.setItem('arepo_camera_lab_renderer_v002',renderMode.value);
  meshRequestSequence++;meshLastKey=null;meshLastReport=null;meshDisplayedCamera=null;meshImage.style.display='none';rulerPoints=[];rulerKind=null;rulerProjection=null;meshFailureKey=null;
  if(nativeMode()){
    compareReturnMode=renderMode.value;
    // Reuse only the already-decoded image for this exact scene/camera/style.
    // A pending response may have changed src before decode; never restore it
    // using the previous frame's report in that case.
    if(meshCachedFrame&&meshCachedFrame.key===meshKey(meshParameters())&&meshCachedFrame.src===meshImage.src){
      meshLastKey=meshCachedFrame.key;meshLastReport=meshCachedFrame.report;meshDisplayedCamera=meshCachedFrame.camera;
      meshNotice.textContent=meshCachedFrame.notice;meshImage.style.display='block';
    }
  }
  syncNativeControls();updateMeasurements();
};
meshOpacityMode.onchange=()=>{if(densityCells()){if(!(meshZoomReferenceHalfCm>0))meshZoomReferenceHalfCm=camera.scale*DATA.scene.display_radius_cm;volumePhysicalTransfer.checked=true;volumeClampColorRange.checked=true;}renderMode.onchange();markStyleDirty();};
meshZoomOpacity.onchange=()=>{if(meshZoomOpacity.checked)meshZoomReferenceHalfCm=camera.scale*DATA.scene.display_radius_cm;meshLastKey=null;meshFailureKey=null;rulerPoints=[];rulerKind=null;rulerProjection=null;syncNativeControls();markStyleDirty();};
comparePoints.onclick=()=>{
  if(!meshLive)return;
  renderMode.value=nativeMode()?'points':compareReturnMode;
  renderMode.onchange();markStyleDirty();
};
document.getElementById('meshRetry').onclick=()=>{meshFailureKey=null;meshLastKey=null;requestNativeFrame(true);};
meshFit.onclick=()=>{markCameraModified();meshFitRequested=true;meshFailureKey=null;};
document.getElementById('clearRuler').onclick=()=>{rulerPoints=[];rulerKind=null;rulerProjection=null;updateMeasurements();syncNativeControls();};
rulerToggle.onchange=()=>{canvas.style.cursor=rulerToggle.checked?'crosshair':'grab';rulerStatus.textContent=rulerToggle.checked?'Click two points to measure.':'Ruler is off.';};
const nativeProgressTimer=setInterval(async()=>{if(!meshBusy)return;try{const response=await fetch('/api/mesh/status',{cache:'no-store'}),state=await response.json();if(meshBusy&&state.message)meshNotice.textContent=state.message;}catch(error){}},1000);
window.addEventListener('pagehide',()=>clearInterval(nativeProgressTimer));

function legendColor(t,style=meshStyle()){
  if(style.invert)t=1-t;t=t**(1/Math.max(style.gamma,.01));
  const stops={copper_blue:[[0,[.025,.055,.10]],[.34,[.16,.38,.55]],[.72,[.72,.34,.12]],[1,[1,.88,.62]]],blue_red:[[0,[.12,.42,.88]],[.5,[.82,.85,.84]],[1,[.88,.30,.10]]]};
  let rows=stops[style.palette];
  if(!rows){const hex=paletteGradients[style.palette].split(',').map(c=>c.length===4?'#'+[...c.slice(1)].map(x=>x+x).join(''):c);rows=hex.map((c,i)=>[i/(hex.length-1),[1,3,5].map(k=>parseInt(c.slice(k,k+2),16)/255)]);}
  let j=0;while(j<rows.length-2&&t>rows[j+1][0])j++;
  const f=(t-rows[j][0])/(rows[j+1][0]-rows[j][0]),rgb=rows[j][1].map((x,i)=>x+(rows[j+1][1][i]-x)*f),luma=rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;
  return 'rgb('+rgb.map(x=>Math.round(Math.min(1,Math.max(0,(luma+(x-luma)*style.saturation)*style.brightness))*255)).join(',')+')';
}
let lastLegendKey='';
function updateMeasurements(){
  measurementHud.style.display=showAnnotations.checked?'block':'none';
  const rect=canvas.getBoundingClientRect(),shown=nativeMode()&&meshDisplayedCamera?meshDisplayedCamera:camera,aspect=nativeMode()&&meshLastReport?meshLastReport.width/meshLastReport.height||canvas.width/canvas.height:canvas.width/canvas.height;
  const bar=CameraMeasurements.scaleBar(shown.scale*DATA.scene.display_radius_cm,aspect,rect.width,140);
  timeLabel.textContent=`t = ${Number(DATA.scene.snapshot_time_seconds).toLocaleString('en-US',{maximumFractionDigits:3})} s · snapshot ${DATA.scene.snapshot??'unknown'}`;
  scaleLabel.textContent=CameraMeasurements.formatLength(bar.lengthCm,measureUnit.value);scaleLine.style.width=bar.pixels+'px';
  const style=nativeMode()&&meshLastReport?meshLastReport.style:meshStyle(),legendKey=JSON.stringify(style),meta=DATA.channels[style.channel]??currentChannel;
  if(legendKey!==lastLegendKey){lastLegendKey=legendKey;legendTitle.textContent=`${meta.label} [${meta.units}] · ${style.scale_mode}`;legendLow.textContent=Number(style.low).toExponential(2);legendHigh.textContent=Number(style.high).toExponential(2);legendGradient.style.background=`linear-gradient(90deg,${Array.from({length:65},(_,i)=>legendColor(i/64,style)).join(',')})`;}
  rulerOverlay.style.left=rect.left+'px';rulerOverlay.style.top=rect.top+'px';rulerOverlay.setAttribute('width',rect.width);rulerOverlay.setAttribute('height',rect.height);rulerOverlay.replaceChildren();
  const points=rulerPoints.map(p=>CameraMeasurements.project(p.position_cm,shown,aspect,DATA.scene.display_radius_cm,DATA.scene.center_cm,rect.width,rect.height));
  function svg(name,attrs){const e=document.createElementNS('http://www.w3.org/2000/svg',name);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);rulerOverlay.appendChild(e);return e;}
  if(points.length===2){svg('line',{x1:points[0][0],y1:points[0][1],x2:points[1][0],y2:points[1][1],stroke:'#fff0ad','stroke-width':2});const text=svg('text',{x:(points[0][0]+points[1][0])/2,y:(points[0][1]+points[1][1])/2-10,fill:'#fff0ad','font-size':13,'text-anchor':'middle',stroke:'#070b0f','stroke-width':4,'paint-order':'stroke'});text.textContent=CameraMeasurements.formatLength(CameraMeasurements.distance(rulerPoints[0].position_cm,rulerPoints[1].position_cm),measureUnit.value)+(rulerKind==='3d'?' (3D)':' (projected)');rulerStatus.textContent=text.textContent;}
  for(const p of points)svg('circle',{cx:p[0],cy:p[1],r:4,fill:'#fff0ad',stroke:'#070b0f','stroke-width':1});
}
async function rulerPick(event){
  if(rulerPicking)return;
  const rect=canvas.getBoundingClientRect(),x=(event.clientX-rect.left)/rect.width,y=(event.clientY-rect.top)/rect.height;
  try{
    let point,kind;
    if(nativeMode()&&(meshBusy||meshLastKey!==meshKey(meshParameters())))throw new Error('Wait for the current native frame before picking.');
    if(surfaceCells()){
      rulerPicking=true;const response=await fetch('/api/mesh/pick',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scene_sha256:DATA.scene.sha256,camera:meshDisplayedCamera,x,y})}),data=await response.json();
      if(!response.ok)throw new Error(data.error||'Mesh pick failed');if(!data.pick.hit)throw new Error('No visible cell under that point.');point=data.pick;kind='3d';
    }else{
      const shown=nativeMode()?meshDisplayedCamera:camera,aspect=nativeMode()?meshLastReport.width/meshLastReport.height||canvas.width/canvas.height:canvas.width/canvas.height;
      const projection={camera:cloned(shown),aspect};
      if(rulerKind==='projected'&&JSON.stringify(rulerProjection)!==JSON.stringify(projection))rulerPoints=[];
      rulerProjection=projection;point={position_cm:CameraMeasurements.pointOnPlane(x,y,shown,aspect,DATA.scene.display_radius_cm,DATA.scene.center_cm)};kind='projected';
    }
    if(rulerPoints.length===2||rulerKind!==kind)rulerPoints=[];rulerKind=kind;rulerPoints.push(point);
    if(rulerPoints.length===1)rulerStatus.textContent='First point selected; click a second point.';
    updateMeasurements();
  }catch(error){rulerStatus.textContent=error.message;}
  finally{rulerPicking=false;}
}
const originalPointerDown=canvas.onpointerdown,originalDoubleClick=canvas.ondblclick;
canvas.onpointerdown=event=>{if(rulerToggle.checked){event.preventDefault();rulerPick(event);return;}noteNativeInteraction();originalPointerDown(event);};
canvas.ondblclick=event=>{if(!rulerToggle.checked)originalDoubleClick(event);};
const originalPointerUp=canvas.onpointerup;
canvas.onpointerup=event=>{if(canvas.hasPointerCapture(event.pointerId)){noteNativeInteraction();originalPointerUp(event);}};
const originalPointerMove=canvas.onpointermove,originalWheel=canvas.onwheel;
canvas.onpointermove=event=>{if(dragging)noteNativeInteraction();originalPointerMove(event);};
canvas.onwheel=event=>{noteNativeInteraction();originalWheel(event);};
window.cameraLabMeasurements=()=>({schema:'arepo_camera_lab_measurements_v001',snapshot:DATA.scene.snapshot,scene_sha256:DATA.scene.sha256,kind:rulerKind,renderer:renderMode.value,projection:rulerKind==='projected'?cloned(rulerProjection):null,points:cloned(rulerPoints),distance_cm:rulerPoints.length===2?CameraMeasurements.distance(rulerPoints[0].position_cm,rulerPoints[1].position_cm):null});
function meshViewState(){return {schema:'arepo_camera_lab_mesh_view_v001',renderer:renderMode.value,cell_opacity_mode:meshOpacityMode.value,zoom_opacity:{enabled:meshZoomOpacity.checked,reference_half_extent_cm:meshZoomReferenceHalfCm??camera.scale*DATA.scene.display_radius_cm},density_floor:+meshDensityFloor.value,volume:volumeState(),volume_profile:volumeProfile.value,subpixel_samples:+volumeQuality.value,edges:meshEdges.checked,interior_faces:meshInterior.checked,lighting:meshLighting.checked,annotations:showAnnotations.checked,measurement_unit:measureUnit.value};}
function applyMeshViewState(state){
  if(!state||state.schema!=='arepo_camera_lab_mesh_view_v001')return;
  renderMode.value=state.renderer==='volume'&&volumeLive?'volume':state.renderer==='mesh'&&meshLive?'mesh':'points';
  meshOpacityMode.value=volumeLive&&state.cell_opacity_mode==='density'?'density':'uniform';
  meshZoomOpacity.checked=Boolean(state.zoom_opacity?.enabled??true);
  const reference=Number(state.zoom_opacity?.reference_half_extent_cm);
  meshZoomReferenceHalfCm=Number.isFinite(reference)&&reference>0?reference:null;
  meshDensityFloor.value=String(state.density_floor);meshEdges.checked=Boolean(state.edges);meshInterior.checked=Boolean(state.interior_faces);meshLighting.checked=Boolean(state.lighting);showAnnotations.checked=Boolean(state.annotations);measureUnit.value=state.measurement_unit||'auto';
  if(state.volume){volumeDensityReference.value=String(state.volume.density_reference);volumeOpacityLength.value=String(state.volume.opacity_length_cm/1e5);volumeDensityPower.value=String(state.volume.density_power);volumeFloorSoftening.value=String(state.volume.floor_softening_dex??1);volumeReconstruction.value=state.volume.reconstruction||'piecewise_constant';volumeProfile.value=state.volume_profile||'custom';volumeQuality.value=String(state.subpixel_samples||4);volumePhysicalTransfer.checked=state.volume.transfer_stage==='after_reconstruction';volumeClampColorRange.checked=state.volume.range_behavior==='clamp';volumeDenseFadeStart.value=String(state.volume.dense_fade_start??0);volumeDenseOpacityFraction.value=String(state.volume.dense_opacity_fraction??1);}
  meshRequestSequence++;meshLastKey=null;meshLastReport=null;meshDisplayedCamera=null;meshImage.style.display='none';rulerPoints=[];rulerKind=null;rulerProjection=null;syncNativeControls();
}
for(const control of [renderMode,meshOpacityMode,meshZoomOpacity,meshDensityFloor,meshEdges,meshInterior,meshLighting,showAnnotations,measureUnit,volumeProfile,volumeDensityReference,volumeOpacityLength,volumeDensityPower,volumeQuality,volumeFloorSoftening,volumeReconstruction,volumePhysicalTransfer,volumeClampColorRange,volumeDenseFadeStart,volumeDenseOpacityFraction]){control.addEventListener('input',markStyleDirty);control.addEventListener('change',markStyleDirty);}
for(const control of [meshDensityFloor,volumeDensityReference,volumeOpacityLength,volumeDensityPower,volumeFloorSoftening,volumeDenseFadeStart,volumeDenseOpacityFraction,opacity,gamma,saturation,brightness])control.addEventListener('input',noteNativeInteraction);
