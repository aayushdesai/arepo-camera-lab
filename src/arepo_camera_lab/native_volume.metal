// Display integration through each native cell; not a radiation-transport model.
#include <metal_stdlib>
using namespace metal;

struct Edge { packed_float3 delta; uint neighbor; };
struct KDNode { float split; uint axis, left, right, first, last, pad0, pad1; };
struct Uniforms { uint4 shape; float4 target, forward, right, up; uint4 scene; };
struct Transfer {float4 domain, density, flags, bounds;};
struct Gradient {float4 scalar, extinction, bounds;};
struct Fit {float3 a,b,c,rhs;uint count;};

void addFit(thread Fit& fit,float3 d,float difference) {
  float d2=dot(d,d);
  if(!(d2>1e-30f)||!isfinite(difference))return;
  float w=1/d2;
  fit.a+=d.x*d*w;fit.b+=d.y*d*w;fit.c+=d.z*d*w;
  fit.rhs+=d*difference*w;fit.count++;
}

bool solveFit(thread const Fit& fit,thread float3& gradient) {
  float3 bc=cross(fit.b,fit.c),ca=cross(fit.c,fit.a),ab=cross(fit.a,fit.b);
  float determinant=dot(fit.a,bc),scale=(fit.a.x+fit.b.y+fit.c.z)/3;
  if(fit.count<3 || determinant<=1e-6f*scale*scale*scale) {gradient=0;return false;}
  gradient=float3(dot(bc,fit.rhs),dot(ca,fit.rhs),dot(ab,fit.rhs))/determinant;
  if(!all(isfinite(gradient))){gradient=0;return false;}
  return true;
}

kernel void prepareGradients(device const uint* offsets [[buffer(0)]],
                             device const Edge* edges [[buffer(1)]],
                             device const float2* fields [[buffer(2)]],
                             device Gradient* gradients [[buffer(3)]],
                             device atomic_uint* failures [[buffer(4)]],
                             constant uint& count [[buffer(5)]],
                             constant float& edgeScale [[buffer(6)]],
                             uint cell [[thread_position_in_grid]]) {
  if(cell>=count)return;
  float2 value=fields[cell],low=value,high=value;
  Fit scalar{},extinction{};
  for(uint e=offsets[cell];e<offsets[cell+1];e++) {
    uint other=(edges[e].neighbor&0x7fffffff)-1;
    if(other>=count)continue; // Traversal rejects this malformed graph explicitly.
    float3 d=float3(edges[e].delta)*edgeScale;
    float2 next=fields[other];
    addFit(scalar,d,next.x-value.x);addFit(extinction,d,next.y-value.y);
    if(isfinite(next.x)){low.x=min(low.x,next.x);high.x=max(high.x,next.x);}
    low.y=min(low.y,next.y);high.y=max(high.y,next.y);
  }
  float3 gs=0,gk=0;
  bool validS=!isfinite(value.x)||solveFit(scalar,gs),validK=solveFit(extinction,gk);
  if(!validS||!validK)atomic_fetch_add_explicit(failures,1u,memory_order_relaxed);
  float2 limit=1;
  for(uint e=offsets[cell];e<offsets[cell+1];e++) {
    float3 d=float3(edges[e].delta)*edgeScale;
    float2 delta=float2(dot(gs,d),dot(gk,d));
    for(uint k=0;k<2;k++) {
      float ratio=delta[k]>0?(high[k]-value[k])/delta[k]:delta[k]<0?(low[k]-value[k])/delta[k]:1;
      limit[k]=min(limit[k],clamp(ratio,0.0f,1.0f));
    }
  }
  gradients[cell]={float4(gs*limit.x,validS?1:0),float4(gk*limit.y,validK?1:0),
                   float4(low.x,high.x,low.y,high.y)};
}

float2 linearFields(float3 relative,uint cell,device const float2* fields,
                    device const Gradient* gradients) {
  if(!isfinite(fields[cell].x))return float2(NAN,0);
  Gradient g=gradients[cell];
  float2 value=fields[cell]+float2(dot(g.scalar.xyz,relative),dot(g.extinction.xyz,relative));
  value.x=clamp(value.x,g.bounds.x,g.bounds.y);
  value.y=clamp(value.y,g.bounds.z,g.bounds.w);
  return value;
}

// Optional field-first transfer. Input is (channel/channel_scale, rho/rho_ref).
// Interpolation therefore precedes nonlinear colour and extinction mapping.
float fieldTransform(float value,constant Transfer& transfer) {
  if(transfer.domain.w<0.5f)return value;
  if(transfer.domain.w<1.5f)return value>0?log10(value):NAN;
  return sign(value)*log10(1+abs(value)/transfer.domain.z);
}

float2 displayTransfer(float2 field,constant Transfer& transfer) {
  if(transfer.flags.x<0.5f)return field;
  // Preserve invalid-density/interpolation failures for the caller's error gate.
  if(!isfinite(field.y)||field.y<0)return float2(NAN,NAN);
  if(!isfinite(field.x)||field.y==0)return float2(NAN,0);
  if(transfer.flags.y>0.5f && (field.x<transfer.domain.x||field.x>transfer.domain.y))
    return float2(NAN,0);
  float scalar=fieldTransform(field.x,transfer);
  if(!isfinite(scalar))return float2(NAN,0);
  float lower=fieldTransform(transfer.domain.x,transfer),upper=fieldTransform(transfer.domain.y,transfer);
  scalar=(scalar-lower)/(upper-lower);
  float support=field.y>=transfer.density.x?1.0f:0.0f;
  if(transfer.density.x>0 && transfer.density.y>0) {
    float ramp=clamp(log10(field.y/transfer.density.x)/transfer.density.y,0.0f,1.0f);
    support=ramp*ramp*(3-2*ramp);
  }
  if(transfer.flags.z>0) {
    float ramp=clamp(log10(field.y/transfer.flags.z),0.0f,1.0f);
    support*=1-(1-transfer.flags.w)*ramp*ramp*(3-2*ramp);
  }
  float extinction=transfer.density.w*pow(field.y,transfer.density.z)*support;
  return float2(scalar,extinction);
}

struct NearestNine { float distance[9]; uint cell[9]; };

// The k nearest sites form a connected subgraph of a Voronoi neighbour graph.
// Expand the nearest unvisited site until the eight positively weighted sites
// are settled; the ninth site sets the zero-weight support boundary.
float2 continuousFields(float3 point, float box, device const float4* positions,
                        device const uint* offsets, device const Edge* edges,
                        device const float2* fields,uint seed,uint count,
                        device const Gradient* gradients,bool blendGradients,
                        constant Transfer& transfer) {
  NearestNine nearest;bool expanded[9];
  for(uint k=0;k<9;k++){nearest.distance[k]=INFINITY;nearest.cell[k]=0xffffffff;expanded[k]=false;}
  float3 d=positions[seed].xyz-point;d-=round(d/box)*box;
  nearest.cell[0]=seed;nearest.distance[0]=dot(d,d);
  bool complete=false;
  for(uint iteration=0;iteration<32;iteration++) {
    uint slot=9;
    for(uint k=0;k<8;k++)if(nearest.cell[k]!=0xffffffff&&!expanded[k]){slot=k;break;}
    if(slot==9){complete=true;break;}
    uint parent=nearest.cell[slot];expanded[slot]=true;
    for(uint e=offsets[parent];e<offsets[parent+1];e++) {
      uint candidate=(edges[e].neighbor&0x7fffffff)-1;
      if(candidate>=count)return float2(NAN,NAN);
      d=positions[candidate].xyz-point;d-=round(d/box)*box;
      float d2=dot(d,d);
      if(d2>=nearest.distance[8])continue;
      bool duplicate=false;
      for(uint k=0;k<9;k++)if(nearest.cell[k]==candidate){duplicate=true;break;}
      if(duplicate)continue;
      uint insert=8;
      while(insert>0 && d2<nearest.distance[insert-1]) {
        nearest.distance[insert]=nearest.distance[insert-1];nearest.cell[insert]=nearest.cell[insert-1];
        expanded[insert]=expanded[insert-1];--insert;
      }
      nearest.distance[insert]=d2;nearest.cell[insert]=candidate;expanded[insert]=false;
    }
  }
  if(!complete || nearest.cell[min(count,9u)-1]==0xffffffff)return float2(NAN,NAN);
  if(nearest.distance[0]==0)return fields[nearest.cell[0]];
  float closest=sqrt(nearest.distance[0]),radius=sqrt(nearest.distance[8]);
  float2 sum=0;float total=0,colorWeight=0;
  for(uint k=0;k<9;k++)if(nearest.cell[k]!=0xffffffff) {
    float d=sqrt(nearest.distance[k]);
    float a=closest/d-closest/radius,weight=a*a;
    uint id=nearest.cell[k];
    float3 relative=point-positions[id].xyz;relative-=round(relative/box)*box;
    float2 value=fields[id];
    if(blendGradients) {
      // Blend local linear polynomials, not independently clipped cell fields.
      // Fixed global bounds preserve continuity as the support changes.
      value+=float2(dot(gradients[id].scalar.xyz,relative),dot(gradients[id].extinction.xyz,relative));
    }
    if(isfinite(value.x)&&fields[id].y>0){sum.x+=weight*value.x;colorWeight+=weight;}
    sum.y+=weight*value.y;total+=weight;
  }
  if(!(total>0))return fields[nearest.cell[0]];
  float2 result=float2(colorWeight>0?sum.x/colorWeight:NAN,sum.y/total);
  if(blendGradients) {
    result.x=isfinite(result.x)?clamp(result.x,transfer.bounds.x,transfer.bounds.y):NAN;
    result.y=clamp(result.y,transfer.bounds.z,transfer.bounds.w);
  }
  return result;
}

uint nearestCell(float3 point, device const float4* positions,
                 device const KDNode* nodes, device const uint* order, uint root) {
  uint pending[64], top=0, current=root, found=0;
  float distances[64], best=INFINITY;
  while(true) {
    KDNode node=nodes[current];
    if(node.axis==3) {
      for(uint j=node.first;j<node.last;j++) {
        uint cell=order[j]; float3 d=positions[cell].xyz-point;float d2=dot(d,d);
        if(d2<best) {best=d2;found=cell;}
      }
      bool more=false;
      while(top) {--top;if(distances[top]<=best){current=pending[top];more=true;break;}}
      if(!more)break;
    } else {
      float d=point[node.axis]-node.split;
      uint near=d<0?node.left:node.right,far=d<0?node.right:node.left;
      if(d*d<=best && top<64) {pending[top]=far;distances[top++]=d*d;}
      current=near;
    }
  }
  return found;
}

uint periodicNearest(float3 point,float box,device const float4* positions,
                     device const KDNode* nodes,device const uint* order,uint root) {
  point-=round(point/box)*box;
  uint found=nearestCell(point,positions,nodes,order,root);
  float3 d=positions[found].xyz-point;
  float best=dot(d,d);float3 boundary=box/2-abs(point);
  for(uint mask=1;mask<8;mask++) {
    float bound=0;float3 other=point;
    for(uint axis=0;axis<3;axis++)if(mask&(1u<<axis)) {
      bound+=boundary[axis]*boundary[axis];other[axis]+=point[axis]>=0?-box:box;
    }
    if(bound>best)continue;
    uint candidate=nearestCell(other,positions,nodes,order,root);
    d=positions[candidate].xyz-other;
    if(dot(d,d)<best){best=dot(d,d);found=candidate;}
  }
  return found;
}

// Direct sampling makes reconstruction checks independent of ray quadrature.
kernel void sampleFields(device const float4* positions [[buffer(0)]],
                          device const uint* offsets [[buffer(1)]],
                          device const Edge* edges [[buffer(2)]],
                          device const KDNode* nodes [[buffer(3)]],
                          device const uint* order [[buffer(4)]],
                          device const float2* fields [[buffer(5)]],
                          device const float4* points [[buffer(6)]],
                          device float2* output [[buffer(7)]],
                          constant uint4& counts [[buffer(8)]],
                          constant float& box [[buffer(9)]],
                          device const Gradient* gradients [[buffer(10)]],
                          constant Transfer& transfer [[buffer(11)]],
                          constant uint& applyTransfer [[buffer(12)]],
                          uint index [[thread_position_in_grid]]) {
  if(index>=counts.x)return;
  float3 point=points[index].xyz;
  uint seed=periodicNearest(point,box,positions,nodes,order,counts.z);
  float3 relative=point-positions[seed].xyz;relative-=round(relative/box)*box;
  float2 result=counts.w==2?linearFields(relative,seed,fields,gradients):
    (counts.w==1||counts.w==3)?continuousFields(point,box,positions,offsets,edges,fields,seed,counts.y,gradients,counts.w==3,transfer):fields[seed];
  output[index]=applyTransfer?displayTransfer(result,transfer):result;
}

// Compensated accumulation prevents many short steps losing their distance
// after entering from a large box boundary. Ray origins lie in the focus plane.
float2 advance(float2 current, float step) {
  float sum=current.x+step, b=sum-current.x;
  float error=(current.x-(sum-b))+(step-b)+current.y;
  float total=sum+error;
  return float2(total,error-(total-sum));
}

kernel void nativeVolume(device const float4* positions [[buffer(0)]],
                         device const uint* offsets [[buffer(1)]],
                         device const Edge* edges [[buffer(2)]],
                         device const KDNode* nodes [[buffer(3)]],
                         device const uint* order [[buffer(4)]],
                         device const float2* fields [[buffer(5)]],
                         device const float4* palette [[buffer(6)]],
                         device float4* pixels [[buffer(7)]],
                         device uint4* statistics [[buffer(8)]],
                         constant Uniforms& u [[buffer(9)]],
                         device const Gradient* gradients [[buffer(10)]],
                         constant Transfer& transfer [[buffer(11)]],
                         uint index [[thread_position_in_grid]]) {
  uint width=u.shape.x,height=u.shape.y,spp=u.shape.z;
  if(index>=width*height*spp)return;
  uint pixel=index/spp, sample=index%spp;
  float2 offset=spp==4?float2((sample%2)*0.5f+0.25f,(sample/2)*0.5f+0.25f):float2(0.5f);
  float2 screen=float2((pixel%width+offset.x)/width,(pixel/width+offset.y)/height);
  float3 direction=u.forward.xyz;
  float3 origin=u.target.xyz+(2*screen.x-1)*u.target.w*u.forward.w*u.right.xyz
                                  +(1-2*screen.y)*u.target.w*u.up.xyz;
  float box=u.right.w, edgeScale=u.up.w;
  float entry=-INFINITY, exit=INFINITY;
  bool intersects=true;
  for(uint axis=0;axis<3;axis++) {
    if(abs(direction[axis])<1e-12f) {
      if(abs(origin[axis])>box/2)intersects=false;
    } else {
      float a=(-box/2-origin[axis])/direction[axis],b=(box/2-origin[axis])/direction[axis];
      entry=max(entry,min(a,b));exit=min(exit,max(a,b));
    }
  }
  float3 background=float3(0.00212469f,0.00334654f,0.00477695f); // sRGB #070b0f in linear light.
  if(!intersects||exit<=entry) {pixels[index]=float4(background,0);statistics[index]=0;return;}
  float3 start=fma(entry,direction,origin);
  uint cell=periodicNearest(start,box,positions,nodes,order,u.scene.y),previous=0xffffffff;
  float2 distance=float2(entry,0);
  float3 color=0;
  float transmission=1;
  uint visits=0,status=0,zeros=0,totalZeros=0;
  for(uint iteration=0;iteration<u.shape.w;iteration++) {
    if(cell>=u.scene.x){status=1;break;}
    visits++;
    float3 relative=fma(distance.x,direction,origin-positions[cell].xyz)+distance.y*direction;
    relative-=round(relative/box)*box;
    float length=INFINITY;uint next=0xffffffff;
    for(uint e=offsets[cell];e<offsets[cell+1];e++) {
      Edge edge=edges[e]; float3 delta=float3(edge.delta)*edgeScale;
      float denom=dot(direction,delta);
      if(denom<=0)continue;
      uint candidate=(edge.neighbor&0x7fffffff)-1;
      float segment=(0.5f*dot(delta,delta)-dot(relative,delta))/denom;
      if(candidate==previous && segment<=0)continue;
      segment=max(0.0f,segment);
      if(segment<length){length=segment;next=candidate;}
    }
    if(!isfinite(length)){status=4;break;}
    float remaining=(exit-distance.x)-distance.y;
    length=min(length,remaining);
    if(length>0) {
      uint samples=u.scene.z?u.scene.w:1;
      for(uint sample=0;sample<samples;sample++) {
        float fraction=samples==2?(sample==0?0.2113248654f:0.7886751346f):0.5f;
        float3 point=fma(distance.x,direction,origin)+(distance.y+length*fraction)*direction;
        float2 field=u.scene.z==2?linearFields(relative+length*fraction*direction,cell,fields,gradients):
          (u.scene.z==1||u.scene.z==3)?continuousFields(point,box,positions,offsets,edges,fields,cell,u.scene.x,gradients,u.scene.z==3,transfer):fields[cell];
        field=displayTransfer(field,transfer);
        if(!isfinite(field.y)||field.y<0){status=64;break;}
        if(field.y>0 && isfinite(field.x)) {
          float tau=min(80.0f,field.y*length/samples);
          float alpha=tau<0.001f?tau*(1-0.5f*tau+tau*tau/6):1-exp(-tau);
          float v=clamp(field.x,0.0f,1.0f)*511;
          uint left=min(uint(v),510u);
          float3 emission=mix(palette[left].xyz,palette[left+1].xyz,v-left);
          color+=transmission*alpha*emission;
          transmission*=1-alpha;
        }
      }
      if(status)break;
      zeros=0;
    } else {zeros++;totalZeros++;}
    if(zeros>16){status=32;break;}
    if(length>=remaining || transmission<0.001f)break;
    if(next>=u.scene.x){status=2;break;}
    distance=advance(distance,length);
    previous=cell;cell=next;
    if(iteration+1==u.shape.w)status=8;
  }
  color+=transmission*background;
  if(!all(isfinite(color)))status|=16;
  pixels[index]=float4(color,1-transmission);
  statistics[index]=uint4(status,visits,totalZeros,0);
}
