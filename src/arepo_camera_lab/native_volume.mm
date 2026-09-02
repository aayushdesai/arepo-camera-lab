// Metal compute bridge for display-only integration through prepared Voronoi cells.
// No simulation IO, field derivation, or voxelization takes place here.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <numeric>
#include <stdexcept>
#include <string>
#include <vector>

struct KDNode { float split; uint32_t axis, left, right, first, last, pad[2]; };
struct Uniforms { uint32_t shape[4]; float camera[16]; uint32_t scene[4]; };
static_assert(sizeof(KDNode) == 32 && sizeof(Uniforms) == 96, "Metal buffer layout");

struct Volume {
  id<MTLDevice> device;
  id<MTLCommandQueue> queue;
  id<MTLComputePipelineState> pipeline, samplePipeline;
  id<MTLBuffer> positions, offsets, edges, nodes, order, fields, palette;
  uint32_t count, root;
  std::string deviceName;
};

static void errorMessage(char* buffer, size_t size, const char* message) {
  if(size) std::snprintf(buffer, size, "%s", message);
}

static id<MTLBuffer> buffer(Volume* v, const void* data, size_t bytes) {
  id<MTLBuffer> result = [v->device newBufferWithBytes:data length:bytes options:MTLResourceStorageModeShared];
  if(!result) throw std::runtime_error("Metal shared buffer allocation failed");
  return result;
}

static uint32_t buildTree(const float* positions, std::vector<uint32_t>& order,
                          std::vector<KDNode>& nodes, uint32_t first, uint32_t last, unsigned depth) {
  uint32_t index = uint32_t(nodes.size());
  nodes.emplace_back();
  KDNode node{};
  node.first = first; node.last = last; node.axis = 3;
  if(last - first > 24) {
    node.axis = depth % 3;
    const uint32_t middle = first + (last-first)/2, axis = node.axis;
    std::nth_element(order.begin()+first, order.begin()+middle, order.begin()+last,
                     [=](uint32_t a, uint32_t b) {return positions[a*4+axis] < positions[b*4+axis];});
    node.split = positions[order[middle]*4+axis];
    node.left = buildTree(positions, order, nodes, first, middle, depth+1);
    node.right = buildTree(positions, order, nodes, middle, last, depth+1);
  }
  nodes[index] = node;
  return index;
}

extern "C" void* av_create(const char* shaderPath, const float* positions,
                            const uint32_t* offsets, const void* edges,
                            uint32_t count, uint32_t edgeCount,
                            char* error, size_t errorSize) {
  @autoreleasepool {
    Volume* v = nullptr;
    try {
      if(!count || !edgeCount) throw std::runtime_error("Native volume requires complete connectivity");
      v = new Volume{};
      v->device = MTLCreateSystemDefaultDevice();
      if(!v->device) throw std::runtime_error("No native Metal device is available; select the VTK face view");
      v->deviceName = [[v->device name] UTF8String];
      v->queue = [v->device newCommandQueue];
      NSError* problem = nil;
      NSString* source = [NSString stringWithContentsOfFile:[NSString stringWithUTF8String:shaderPath]
                                 encoding:NSUTF8StringEncoding error:&problem];
      if(!source) throw std::runtime_error([[problem localizedDescription] UTF8String]);
      MTLCompileOptions* options = [MTLCompileOptions new];
      // Safe math preserves finite checks and compensated ray distances.
      if(@available(macOS 15.0, *)) {
        options.mathMode = MTLMathModeSafe;
      } else {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
        options.fastMathEnabled = NO;
#pragma clang diagnostic pop
      }
      id<MTLLibrary> library = [v->device newLibraryWithSource:source options:options error:&problem];
      if(!library) throw std::runtime_error([[problem localizedDescription] UTF8String]);
      id<MTLFunction> function = [library newFunctionWithName:@"nativeVolume"];
      v->pipeline = [v->device newComputePipelineStateWithFunction:function error:&problem];
      if(!v->pipeline) throw std::runtime_error([[problem localizedDescription] UTF8String]);
      function = [library newFunctionWithName:@"sampleFields"];
      v->samplePipeline = [v->device newComputePipelineStateWithFunction:function error:&problem];
      if(!v->samplePipeline) throw std::runtime_error([[problem localizedDescription] UTF8String]);
      v->count = count;
      v->positions = buffer(v, positions, size_t(count)*16);
      v->offsets = buffer(v, offsets, size_t(count+1)*4);
      v->edges = buffer(v, edges, size_t(edgeCount)*16);
      std::vector<uint32_t> order(count);
      std::iota(order.begin(), order.end(), 0);
      std::vector<KDNode> nodes;
      nodes.reserve(count/8);
      v->root = buildTree(positions, order, nodes, 0, count, 0);
      v->nodes = buffer(v, nodes.data(), nodes.size()*sizeof(KDNode));
      v->order = buffer(v, order.data(), order.size()*4);
      return v;
    } catch(const std::exception& ex) {
      errorMessage(error, errorSize, ex.what()); delete v; return nullptr;
    }
  }
}

extern "C" const char* av_device(void* handle) {
  return static_cast<Volume*>(handle)->deviceName.c_str();
}

extern "C" int av_fields(void* handle, const float* fields, const float* palette,
                          char* error, size_t errorSize) {
  @autoreleasepool {
    try {
      auto* v = static_cast<Volume*>(handle);
      v->fields = buffer(v, fields, size_t(v->count)*8);
      v->palette = buffer(v, palette, 512*16);
      return 0;
    } catch(const std::exception& ex) {errorMessage(error, errorSize, ex.what());return 1;}
  }
}

extern "C" int av_render(void* handle, uint32_t width, uint32_t height,
                          uint32_t subpixels, uint32_t maxSteps,
                          uint32_t reconstruction, uint32_t cellSamples, const float* camera,
                          float* pixels, uint32_t* stats, double* gpuSeconds,
                          char* error, size_t errorSize) {
  @autoreleasepool {
    try {
      auto* v = static_cast<Volume*>(handle);
      if(!v->fields || !v->palette) throw std::runtime_error("Set the native volume transfer before rendering");
      if(reconstruction>1 || (cellSamples!=1 && cellSamples!=2))
        throw std::runtime_error("Invalid native volume reconstruction or cell sampling");
      const size_t rays = size_t(width)*height*subpixels;
      if(!rays || rays > 1920ul*1200*4) throw std::runtime_error("Native volume viewport exceeds its pixel budget");
      Uniforms u{{width,height,subpixels,maxSteps}, {}, {v->count,v->root,reconstruction,cellSamples}};
      std::memcpy(u.camera, camera, sizeof(u.camera));
      id<MTLBuffer> output = [v->device newBufferWithLength:rays*16 options:MTLResourceStorageModeShared];
      id<MTLBuffer> status = [v->device newBufferWithLength:rays*16 options:MTLResourceStorageModeShared];
      if(!output || !status) throw std::runtime_error("Metal frame allocation failed");
      id<MTLCommandBuffer> command = [v->queue commandBuffer];
      id<MTLComputeCommandEncoder> encoder = [command computeCommandEncoder];
      [encoder setComputePipelineState:v->pipeline];
      NSArray* buffers = @[v->positions,v->offsets,v->edges,v->nodes,v->order,v->fields,v->palette,output,status];
      for(NSUInteger i=0;i<buffers.count;i++) [encoder setBuffer:buffers[i] offset:0 atIndex:i];
      [encoder setBytes:&u length:sizeof(u) atIndex:9];
      NSUInteger group = std::min(NSUInteger(128),v->pipeline.maxTotalThreadsPerThreadgroup);
      [encoder dispatchThreads:MTLSizeMake(rays,1,1) threadsPerThreadgroup:MTLSizeMake(group,1,1)];
      [encoder endEncoding]; [command commit]; [command waitUntilCompleted];
      if(command.status != MTLCommandBufferStatusCompleted)
        throw std::runtime_error([[command.error localizedDescription] UTF8String]);
      std::memcpy(pixels, output.contents, rays*16);
      std::memcpy(stats, status.contents, rays*16);
      *gpuSeconds = command.GPUEndTime-command.GPUStartTime;
      return 0;
    } catch(const std::exception& ex) {errorMessage(error, errorSize, ex.what());return 1;}
  }
}

extern "C" int av_sample_fields(void* handle, const float* points, uint32_t count,
                                 float box, float* values, char* error, size_t errorSize) {
  @autoreleasepool {
    try {
      auto* v=static_cast<Volume*>(handle);
      if(!v->fields || !count || count>1000000 || !(box>0))
        throw std::runtime_error("Invalid native field sample request");
      auto queries=buffer(v,points,size_t(count)*16);
      id<MTLBuffer> output=[v->device newBufferWithLength:size_t(count)*8 options:MTLResourceStorageModeShared];
      if(!output)throw std::runtime_error("Metal sample allocation failed");
      id<MTLCommandBuffer> command=[v->queue commandBuffer];
      id<MTLComputeCommandEncoder> encoder=[command computeCommandEncoder];
      [encoder setComputePipelineState:v->samplePipeline];
      NSArray* buffers=@[v->positions,v->offsets,v->edges,v->nodes,v->order,v->fields,queries,output];
      for(NSUInteger i=0;i<buffers.count;i++)[encoder setBuffer:buffers[i] offset:0 atIndex:i];
      uint32_t counts[4]{count,v->count,v->root,0};
      [encoder setBytes:counts length:sizeof(counts) atIndex:8];
      [encoder setBytes:&box length:sizeof(box) atIndex:9];
      NSUInteger group=std::min(NSUInteger(128),v->samplePipeline.maxTotalThreadsPerThreadgroup);
      [encoder dispatchThreads:MTLSizeMake(count,1,1) threadsPerThreadgroup:MTLSizeMake(group,1,1)];
      [encoder endEncoding];[command commit];[command waitUntilCompleted];
      if(command.status!=MTLCommandBufferStatusCompleted)
        throw std::runtime_error([[command.error localizedDescription] UTF8String]);
      std::memcpy(values,output.contents,size_t(count)*8);
      return 0;
    }catch(const std::exception& ex){errorMessage(error,errorSize,ex.what());return 1;}
  }
}

extern "C" void av_close(void* handle) { @autoreleasepool {delete static_cast<Volume*>(handle);} }
