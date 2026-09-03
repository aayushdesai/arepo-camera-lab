// Manual snapshot browsing carries a physical viewport and display settings.
// It does not rebind an immutable camera pose to a different simulation output.
window.cameraLabInspectionState=()=>{
  const physical=pose(),visual=visualState(physical);
  return {schema:'arepo_camera_lab_inspection_v001',physical,visual,
    channel_units:DATA.channels[visual.channel]?.units};
};
window.cameraLabRestoreInspection=(state,expectedScene)=>{
  if(state?.schema!=='arepo_camera_lab_inspection_v001')throw new Error('Invalid saved inspection view.');
  if(Number(expectedScene?.snapshot)!==Number(DATA.scene.snapshot)||expectedScene?.sha256!==DATA.scene.sha256)
    throw new Error('The requested snapshot is not displayed yet.');
  const visual=state.visual,meta=DATA.channels[visual?.channel];
  if(!meta)throw new Error(`Field ${visual?.channel} is unavailable in this snapshot; the view was not restored.`);
  if(meta.units!==state.channel_units)throw new Error('The field units changed; the view was not restored.');
  const p=state.physical,radius=Number(DATA.scene.display_radius_cm),center=DATA.scene.center_cm;
  const validVector=value=>Array.isArray(value)&&value.length===3&&value.every(Number.isFinite);
  if(!p||![p.look_at_cm,p.view_direction,p.up,center].every(validVector)||!Number.isFinite(radius)||radius<=0||!Number.isFinite(p.screen_half_extent_cm)||p.screen_half_extent_cm<=0)
    throw new Error('The physical camera is invalid; the view was not restored.');
  const target=p.look_at_cm.map((value,index)=>(value-center[index])/radius);
  const nextCamera={target,forward:[...p.view_direction],up:[...p.up],scale:p.screen_half_extent_cm/radius};
  // Keep current canvas dimensions if the window changed during loading.
  resize();
  applyVisualState({...cloned(visual),canvas_size:{width:canvas.width,height:canvas.height}});
  setCamera(nextCamera);markCameraModified();markStyleDirty();
  meshFitRequested=false;nativeInteractiveUntil=0;meshFailureKey=null;
  meshLastKey=null;meshLastReport=null;meshDisplayedCamera=null;meshImage.style.display='none';
  updateMeasurements();
  return {snapshot:DATA.scene.snapshot,scene_sha256:DATA.scene.sha256,channel:visual.channel,renderer:renderMode.value};
};
