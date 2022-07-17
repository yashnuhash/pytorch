#pragma once

#ifdef USE_VULKAN_API

#include <ATen/native/vulkan/api/Adapter.h>
#include <ATen/native/vulkan/api/Command.h>
#include <ATen/native/vulkan/api/Common.h>
#include <ATen/native/vulkan/api/Descriptor.h>
#include <ATen/native/vulkan/api/Pipeline.h>
#include <ATen/native/vulkan/api/QueryPool.h>
#include <ATen/native/vulkan/api/Resource.h>
#include <ATen/native/vulkan/api/Runtime.h>
#include <ATen/native/vulkan/api/Shader.h>

namespace at {
namespace native {
namespace vulkan {
namespace api {

struct ContextConfig final {
  uint32_t cmdSubmitFrequency;
  CommandPoolConfig cmdPoolConfig;
  DescriptorPoolConfig descriptorPoolConfig;
  QueryPoolConfig queryPoolConfig;
};

//
// Vulkan Context holds onto all relevant Vulkan state as it pertains to our
// use of Vulkan in PyTorch.  A Context is associated with one, and only one,
// Adapter as a precursor to multi-GPU support.  All Vulkan tensors in PyTorch
// are associated with a Context to make tensor <-> device affinity explicit.
// The context is currently a global object, but technically it does not need
// to be if we were to make it explicit to the user.
//

class Context final {
 public:
  explicit Context(size_t adapter_i, const ContextConfig&);

  Context(const Context&) = delete;
  Context& operator=(const Context&) = delete;

  Context(Context&&) = delete;
  Context& operator=(Context&&) = delete;

  ~Context();

 private:
  // Config
  ContextConfig config_;
  // Important handles
  Adapter* adapter_p_;
  VkDevice device_;
  Adapter::Queue queue_;
  // Resource Pools
  CommandPool command_pool_;
  DescriptorPool descriptor_pool_;
  FencePool fences_;
  // Diagnostics
#ifdef USE_VULKAN_GPU_DIAGNOSTICS
  QueryPool querypool_;
#endif /* USE_VULKAN_GPU_DIAGNOSTICS */
  // Command buffers submission
  std::mutex cmd_mutex_;
  CommandBuffer cmd_;
  uint32_t submit_count_;
  // Memory Management
  std::mutex buffer_clearlist_mutex_;
  std::vector<VulkanBuffer> buffers_to_clear_;
  std::mutex image_clearlist_mutex_;
  std::vector<VulkanImage> images_to_clear_;

 public:
  // Adapter access

  inline Adapter* adapter_ptr() {
    return adapter_p_;
  }

  inline VkDevice device() {
    return device_;
  }

  inline VkQueue queue() {
    return queue_.handle;
  }

  // Device Caches

  inline ShaderLayoutCache& shader_layout_cache() {
    return adapter_ptr()->shader_layout_cache();
  }

  inline ShaderCache& shader_cache() {
    return adapter_ptr()->shader_cache();
  }

  inline PipelineLayoutCache& pipeline_layout_cache() {
    return adapter_ptr()->pipeline_layout_cache();
  }

  inline ComputePipelineCache& pipeline_cache() {
    return adapter_ptr()->compute_pipeline_cache();
  }

  // Resource Pools

  inline DescriptorPool& descriptor_pool() {
    return descriptor_pool_;
  }

  inline FencePool& fences() {
    return fences_;
  }

  // Diagnostics

#ifdef USE_VULKAN_GPU_DIAGNOSTICS
  inline QueryPool& querypool() {
    return querypool_;
  }

  inline void reset_querypool() {
    set_cmd();
    querypool_.reset(cmd_);
  }
#endif /* USE_VULKAN_GPU_DIAGNOSTICS */

  // Memory Management
  void register_buffer_cleanup(VulkanBuffer& buffer) {
    std::lock_guard<std::mutex> bufferlist_lock(buffer_clearlist_mutex_);
    buffers_to_clear_.emplace_back(std::move(buffer));
  }

  void register_image_cleanup(VulkanImage& image) {
    std::lock_guard<std::mutex> imagelist_lock(image_clearlist_mutex_);
    images_to_clear_.emplace_back(std::move(image));
  }

  // GPU RPC

  inline std::unique_lock<std::mutex> dispatch_lock() {
    return std::unique_lock<std::mutex>(cmd_mutex_);
  }

 private:
  inline void set_cmd() {
    if (!cmd_) {
      cmd_ = command_pool_.get_new_cmd();
      cmd_.begin();
    }
  }

  DescriptorSet submit_compute_prologue(
      CommandBuffer&,
      const ShaderLayout::Signature&,
      const ShaderSource&,
      const utils::uvec3&);

  void submit_compute_epilogue(
      CommandBuffer&,
      const DescriptorSet&,
      const PipelineBarrier&,
      const utils::uvec3&);

 public:
  template <typename... Arguments>
  void submit_compute_job(
      const ShaderLayout::Signature&,
      const ShaderSource&,
      const PipelineBarrier&,
      const utils::uvec3&,
      const utils::uvec3&,
      const VkFence fence_handle,
      Arguments&&...);

  void submit_texture_copy(
      const PipelineBarrier& pipeline_barrier,
      const api::VulkanImage&,
      const api::VulkanImage&,
      const api::utils::uvec3&,
      const api::utils::uvec3&,
      const api::utils::uvec3&,
      const VkFence fence_handle);

 private:
  void submit_cmd_to_gpu(const VkFence fence_handle = VK_NULL_HANDLE);

 public:
  void flush();
};

class UniformParamsBuffer final {
 private:
  Context* context_p_;
  VulkanBuffer vulkan_buffer_;

 public:
  template <typename Block>
  UniformParamsBuffer(Context* context_p, const Block& block)
      : context_p_(context_p),
        vulkan_buffer_(
            context_p_->adapter_ptr()->vma().create_params_buffer(block)) {}

  UniformParamsBuffer(const UniformParamsBuffer&) = delete;
  UniformParamsBuffer& operator=(const UniformParamsBuffer&) = delete;

  UniformParamsBuffer(UniformParamsBuffer&&) = delete;
  UniformParamsBuffer& operator=(UniformParamsBuffer&&) = delete;

  ~UniformParamsBuffer() {
    context_p_->register_buffer_cleanup(vulkan_buffer_);
  }

  VulkanBuffer& buffer() {
    return vulkan_buffer_;
  }
};

class StagingBuffer final {
 private:
  Context* context_p_;
  VulkanBuffer vulkan_buffer_;

 public:
  StagingBuffer(
      Context* context_p,
      const VkDeviceSize size,
      const bool gpuonly = false)
      : context_p_(context_p),
        vulkan_buffer_(context_p_->adapter_ptr()->vma().create_storage_buffer(
            size,
            gpuonly)) {}

  StagingBuffer(const StagingBuffer&) = delete;
  StagingBuffer& operator=(const StagingBuffer&) = delete;

  StagingBuffer(StagingBuffer&&) = delete;
  StagingBuffer& operator=(StagingBuffer&&) = delete;

  ~StagingBuffer() {
    context_p_->register_buffer_cleanup(vulkan_buffer_);
  }

  VulkanBuffer& buffer() {
    return vulkan_buffer_;
  }
};

bool available();

// The global runtime is retrieved using this function, where it is declared as
// a static local variable.
Context* context();

namespace detail {

template <size_t... Indices, typename... Arguments>
inline void bind(
    DescriptorSet& descriptor_set,
    const std::index_sequence<Indices...>,
    Arguments&&... arguments) {
  C10_UNUSED const int _[]{
      0,
      (descriptor_set.bind(Indices, std::forward<Arguments>(arguments)), 0)...,
  };
}

} // namespace detail

template <typename... Arguments>
inline void Context::submit_compute_job(
    const ShaderLayout::Signature& shader_layout_signature,
    const ShaderSource& shader_descriptor,
    const PipelineBarrier& pipeline_barrier,
    const utils::uvec3& global_work_group,
    const utils::uvec3& local_work_group_size,
    const VkFence fence_handle,
    Arguments&&... arguments) {
  // Serialize recording to the shared command buffer. Do not initialize with a
  // mutex just yet, since in some cases it will be externally managed.
  std::unique_lock<std::mutex> cmd_lock;
  // If a fence was passed, then assume that the host intends to sync with
  // the GPU, implying there will be imminent calls to fence.wait() and flush().
  // We therefore assume the mutex is externally managed in this case, and the
  // calling thread has already locked the mutex prior to calling the function,
  // and will release the mutex manually after calling flush(). This will
  // prevent more dispatches from being recorded until we have flushed the
  // Context.
  if (fence_handle == VK_NULL_HANDLE) {
    cmd_lock = std::unique_lock<std::mutex>(cmd_mutex_);
  }

  set_cmd();

#ifdef USE_VULKAN_GPU_DIAGNOSTICS
  uint32_t log_idx = querypool_.shader_profile_begin(
      cmd_,
      shader_descriptor.kernel_name,
      create_extent3d(global_work_group),
      create_extent3d(local_work_group_size));
#endif /* USE_VULKAN_GPU_DIAGNOSTICS */

  // Factor out template parameter independent code to minimize code bloat.
  DescriptorSet descriptor_set = submit_compute_prologue(
      cmd_, shader_layout_signature, shader_descriptor, local_work_group_size);

  detail::bind(
      descriptor_set,
      std::index_sequence_for<Arguments...>{},
      std::forward<Arguments>(arguments)...);

  // Factor out template parameter independent code to minimize code bloat.
  submit_compute_epilogue(
      cmd_, descriptor_set, pipeline_barrier, global_work_group);

#ifdef USE_VULKAN_GPU_DIAGNOSTICS
  querypool_.shader_profile_end(cmd_, log_idx);
#endif /* USE_VULKAN_GPU_DIAGNOSTICS */

  submit_count_++;
  if (fence_handle != VK_NULL_HANDLE ||
      submit_count_ >= config_.cmdSubmitFrequency) {
    submit_cmd_to_gpu(fence_handle);
  }
}

} // namespace api
} // namespace vulkan
} // namespace native
} // namespace at

#endif /* USE_VULKAN_API */
