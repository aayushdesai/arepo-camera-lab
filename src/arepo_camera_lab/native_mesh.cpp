// Native arm64/x86 CPU reconstruction of the faces encoded by AREPO-VTK v052.
// Uses every exported neighbour plane; never tessellates a sampled point cloud.
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#pragma pack(push, 1)
struct Header {
  char magic[16]; uint32_t version, endian, headerBytes, cellBytes, edgeBytes;
  uint32_t rayBytes, sampleWidth, sampleHeight, sourceWidth, sourceHeight;
  int32_t samples; uint32_t flags;
  uint64_t cells, edges, rays, invalidEdges, inactiveRays;
  double box, rayMax, origin[3], positionUnit, densityUnit, velocityUnit;
  double temperatureUnit, time; uint8_t reserved[24];
};
struct Cell { double position[3]; float density, temperature, velocity[3]; uint64_t id; };
struct Edge { float delta[3]; uint32_t neighbor; };
#pragma pack(pop)
static_assert(sizeof(Header)==208 && sizeof(Cell)==52 && sizeof(Edge)==16, "v052 layout");
using V3 = std::array<double, 3>;
using V2 = std::array<double, 2>;
double dot(V3 a,V3 b){return a[0]*b[0]+a[1]*b[1]+a[2]*b[2];}
V3 mul(V3 a,double b){return {a[0]*b,a[1]*b,a[2]*b};}
V3 add(V3 a,V3 b){return {a[0]+b[0],a[1]+b[1],a[2]+b[2]};}
V3 cross(V3 a,V3 b){return {a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]};}
V3 unit(V3 a){double n=std::sqrt(dot(a,a));if(!(n>0))throw std::runtime_error("zero plane normal");return mul(a,1/n);}
struct Polygon { uint32_t owner; std::vector<std::array<float,3>> vertices; };

std::vector<V3> face(const std::vector<V3>& planes, size_t which) {
  const V3 d=planes[which], n=unit(d), origin=mul(d,0.5);
  const V3 u=unit(cross(n,std::abs(n[0])<0.8?V3{1,0,0}:V3{0,1,0}));
  const V3 v=cross(n,u); // u cross v points outwards.
  for(double radius=2;radius<=65536;radius*=4) {
    std::vector<V2> poly{{-radius,-radius},{radius,-radius},{radius,radius},{-radius,radius}};
    for(size_t j=0;j<planes.size() && !poly.empty();++j) {
      if(j==which)continue;
      V3 q=planes[j]; double a=dot(q,u), b=dot(q,v), c=dot(q,q)*0.5-dot(q,origin);
      std::vector<V2> next;
      for(size_t k=0;k<poly.size();++k) {
        V2 p=poly[k], r=poly[(k+1)%poly.size()];
        double fp=a*p[0]+b*p[1]-c, fr=a*r[0]+b*r[1]-c;
        bool pin=fp<=0, rin=fr<=0;
        if(pin)next.push_back(p);
        if(pin!=rin) {double t=fp/(fp-fr); next.push_back({p[0]+t*(r[0]-p[0]),p[1]+t*(r[1]-p[1])});}
      }
      poly.swap(next);
    }
    if(poly.size()<3)return {};
    bool touches=false;
    for(auto p:poly)if(std::max(std::abs(p[0]),std::abs(p[1]))>=radius*(1-1e-10))touches=true;
    if(touches)continue;
    std::vector<V3> result;
    for(auto p:poly) {
      V3 w=add(origin,add(mul(u,p[0]),mul(v,p[1])));
      if(!result.empty()) {V3 z=add(w,mul(result.back(),-1));if(dot(z,z)<1e-24)continue;}
      result.push_back(w);
    }
    if(result.size()>2) {V3 z=add(result.front(),mul(result.back(),-1));if(dot(z,z)<1e-24)result.pop_back();}
    if(result.size()<3)return {};
    return result;
  }
  throw std::runtime_error("unbounded face: native neighbour planes do not close the cell");
}

template<class T> void write(std::ofstream& f,const T* p,size_t count){f.write(reinterpret_cast<const char*>(p),sizeof(T)*count);if(!f)throw std::runtime_error("mesh output write failed");}

int main(int argc,char** argv) {
  try {
    if(argc!=11)throw std::runtime_error("scene mask output center_x center_y center_z radius threads interior max_vertices");
    int fd=open(argv[1],O_RDONLY);if(fd<0)throw std::runtime_error("cannot open scene");
    struct stat st;if(fstat(fd,&st)!=0||st.st_size<208)throw std::runtime_error("scene header is truncated");
    const char* bytes=static_cast<const char*>(mmap(nullptr,st.st_size,PROT_READ,MAP_PRIVATE,fd,0));
    if(bytes==MAP_FAILED)throw std::runtime_error("cannot map scene");
    Header h;std::memcpy(&h,bytes,sizeof(h));
    if(std::memcmp(h.magic,"ARVTKSTARV052A",13)||h.version!=5||h.endian!=0x01020304||h.headerBytes!=208||h.cellBytes!=52||h.edgeBytes!=16||(h.flags&32)||!h.cells||!h.edges||h.invalidEdges)
      throw std::runtime_error("requires complete v052 native connectivity with zero invalid edges");
    if(h.cells>=0x7fffffffu||h.edges>uint64_t(st.st_size)/16||h.cells>uint64_t(st.st_size)/52)throw std::runtime_error("invalid scene counts");
    uint64_t edgeStart=208+h.cells*52+(h.cells+1)*8;
    if(edgeStart+h.edges*16>uint64_t(st.st_size))throw std::runtime_error("native connectivity is truncated");
    const Cell* cells=reinterpret_cast<const Cell*>(bytes+208);
    // offsets can be unaligned: copy rather than casting the packed file.
    std::vector<uint64_t> offsets(h.cells+1);std::memcpy(offsets.data(),bytes+208+h.cells*52,offsets.size()*8);
    if(offsets.front()!=0||offsets.back()!=h.edges||!std::is_sorted(offsets.begin(),offsets.end()))throw std::runtime_error("invalid neighbour offsets");
    const Edge* edges=reinterpret_cast<const Edge*>(bytes+edgeStart);
    std::ifstream maskFile(argv[2],std::ios::binary|std::ios::ate);
    if(!maskFile||maskFile.tellg()!=std::streampos(h.cells))throw std::runtime_error("mask length does not match native cells");
    maskFile.seekg(0);std::vector<uint8_t> mask(h.cells);maskFile.read(reinterpret_cast<char*>(mask.data()),mask.size());
    V3 center{std::stod(argv[4]),std::stod(argv[5]),std::stod(argv[6])};double radius=std::stod(argv[7]);
    if(!(radius>0)||!std::isfinite(radius)||!(h.positionUnit>0)||!std::isfinite(h.positionUnit))throw std::runtime_error("invalid physical scale");
    for(double x:center)if(!std::isfinite(x))throw std::runtime_error("nonfinite centre");
    int threadCount=std::max(1,std::min(16,std::stoi(argv[8])));bool interior=std::stoi(argv[9])!=0;
    uint64_t vertexLimit=std::stoull(argv[10]);
    std::vector<uint32_t> boundary;
    uint64_t selected=0;
    for(uint32_t i=0;i<h.cells;++i)if(mask[i]) {
      ++selected;bool include=interior;
      for(uint64_t e=offsets[i];e<offsets[i+1]&&!include;++e) {uint32_t q=edges[e].neighbor&0x7fffffffu;include=q==0||q>h.cells||!mask[q-1];}
      if(include)boundary.push_back(i);
    }
    std::vector<std::vector<Polygon>> results(boundary.size());
    std::atomic<size_t> next{0};std::atomic<uint64_t> vertices{0},empty{0};std::atomic<bool> failed{false};
    std::string error;std::atomic_flag errorLock=ATOMIC_FLAG_INIT;
    auto worker=[&](){
      try {
        while(!failed) {
          size_t item=next.fetch_add(1);if(item>=boundary.size())break;uint32_t i=boundary[item];
          std::vector<V3> planes;double scale=0;
          for(uint64_t e=offsets[i];e<offsets[i+1];++e) {V3 d{edges[e].delta[0],edges[e].delta[1],edges[e].delta[2]};double norm=std::sqrt(dot(d,d));if(!(norm>0)||!std::isfinite(norm))throw std::runtime_error("nonfinite or zero native edge");scale=std::max(scale,norm);planes.push_back(d);}
          if(planes.size()<4)throw std::runtime_error("cell has fewer than four native planes");
          for(auto& d:planes)d=mul(d,1/scale);
          V3 base;
          for(int k=0;k<3;++k) {base[k]=cells[i].position[k]*h.positionUnit-center[k];if(h.box>0)base[k]-=std::round(base[k]/(h.box*h.positionUnit))*h.box*h.positionUnit;}
          for(size_t j=0;j<planes.size();++j) {
            uint32_t q=edges[offsets[i]+j].neighbor&0x7fffffffu;
            if(q>0&&q<=h.cells&&mask[q-1]&&(!interior||q-1<i))continue;
            auto polygon=face(planes,j);if(polygon.empty()){++empty;continue;}
            if(vertices.fetch_add(polygon.size())+polygon.size()>vertexLimit)throw std::runtime_error("mesh vertex budget exceeded; narrow the visible field range or turn off interior faces");
            Polygon out;out.owner=i;out.vertices.reserve(polygon.size());
            for(auto p:polygon) {std::array<float,3> point;for(int k=0;k<3;++k)point[k]=float((base[k]+p[k]*scale*h.positionUnit)/radius);out.vertices.push_back(point);}
            results[item].push_back(std::move(out));
          }
        }
      } catch(const std::exception& ex) {failed=true;while(errorLock.test_and_set()){}if(error.empty())error=ex.what();errorLock.clear();}
    };
    std::vector<std::thread> threads;for(int i=0;i<threadCount;++i)threads.emplace_back(worker);for(auto& t:threads)t.join();
    if(failed)throw std::runtime_error(error);
    uint64_t polygons=0,points=0;for(auto& group:results)for(auto& p:group){++polygons;points+=p.vertices.size();}
    // Exclusive output: callers give each request a fresh temporary namespace.
    int outfd=open(argv[3],O_WRONLY|O_CREAT|O_EXCL,0600);if(outfd<0)throw std::runtime_error("output exists or cannot be created");close(outfd);
    std::ofstream out(argv[3],std::ios::binary);char magic[16]={};std::memcpy(magic,"ACLMESH0001",11);write(out,magic,16);write(out,&polygons,1);write(out,&points,1);
    for(auto& group:results)for(auto& p:group)write(out,p.vertices.data(),p.vertices.size());
    uint64_t offset=0;write(out,&offset,1);for(auto& group:results)for(auto& p:group){offset+=p.vertices.size();write(out,&offset,1);}
    for(auto& group:results)for(auto& p:group)write(out,&p.owner,1);
    out.close();if(!out)throw std::runtime_error("mesh output close failed");
    std::cout<<"{\"selected_cells\":"<<selected<<",\"boundary_cells\":"<<boundary.size()<<",\"faces\":"<<polygons<<",\"vertices\":"<<points<<",\"empty_native_faces\":"<<empty<<",\"threads\":"<<threadCount<<"}\n";
    munmap(const_cast<char*>(bytes),st.st_size);close(fd);
    return 0;
  } catch(const std::exception& ex) {std::cerr<<ex.what()<<"\n";return 1;}
}
