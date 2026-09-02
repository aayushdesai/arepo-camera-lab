// Display integration through each native cell; not a radiation-transport model.
#include <metal_stdlib>
using namespace metal;

struct Edge { packed_float3 delta; uint neighbor; };
struct KDNode { float split; uint axis, left, right, first, last, pad0, pad1; };
struct Uniforms { uint4 shape; float4 target, forward, right, up; uint4 scene; };

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
  uint cell=nearestCell(start,positions,nodes,order,u.scene.y),previous=0xffffffff;
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
      float2 field=fields[cell];
      if(field.y>0 && isfinite(field.x)) {
        float tau=min(80.0f,field.y*length);
        float alpha=tau<0.001f?tau*(1-0.5f*tau+tau*tau/6):1-exp(-tau);
        float v=clamp(field.x,0.0f,1.0f)*511;
        uint left=min(uint(v),510u);
        float3 emission=mix(palette[left].xyz,palette[left+1].xyz,v-left);
        color+=transmission*alpha*emission;
        transmission*=1-alpha;
      }
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
