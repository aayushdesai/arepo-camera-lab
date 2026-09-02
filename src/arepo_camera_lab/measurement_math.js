// Physical measurements for the orthographic viewer; CSS pixels and device
// pixels deliberately remain separate.
const CameraMeasurements=(()=>{
  const dot=(a,b)=>a.reduce((sum,x,i)=>sum+x*b[i],0);
  function scaleBar(halfCm,aspect,width,maxPixels=140){
    if(!(halfCm>0&&aspect>0&&width>0))throw new Error('Invalid viewport scale');
    const cmPerPixel=2*halfCm*aspect/width,desired=cmPerPixel*Math.min(maxPixels,width*.28);
    const power=10**Math.floor(Math.log10(desired));
    const factor=[5,2,1].find(x=>x*power<=desired)??1;
    const lengthCm=factor*power;return {lengthCm,pixels:lengthCm/cmPerPixel,cmPerPixel};
  }
  function pointOnPlane(x,y,camera,aspect,radius,center){
    return center.map((value,i)=>value+radius*(camera.target[i]+(2*x-1)*camera.scale*aspect*camera.right[i]+(1-2*y)*camera.scale*camera.up[i]));
  }
  function project(point,camera,aspect,radius,center,width,height){
    const relative=point.map((x,i)=>(x-center[i])/radius-camera.target[i]);
    return [(dot(relative,camera.right)/(camera.scale*aspect)+1)*width/2,
            (1-dot(relative,camera.up)/camera.scale)*height/2];
  }
  function distance(a,b){return Math.hypot(...a.map((x,i)=>x-b[i]));}
  function formatLength(cm,unit='auto'){
    if(unit==='auto')unit=Math.abs(cm)>=1e5?'km':'cm';
    const value=unit==='km'?cm/1e5:cm;
    return `${Number(value.toPrecision(4)).toLocaleString('en-US',{maximumSignificantDigits:4})} ${unit}`;
  }
  return {scaleBar,pointOnPlane,project,distance,formatLength};
})();
if(typeof module!=='undefined'&&module.exports)module.exports=CameraMeasurements;
