#include <ATen/native/UpSample.h>
#include <ATen/native/vulkan/ops/Common.h>
#include <torch/library.h>

namespace at {
namespace native {
namespace vulkan {
namespace ops {
namespace {

using namespace api::utils;

Tensor upsample_nearest2d(
    const Tensor& input_arg,
    const IntArrayRef output_sizes,
    const c10::optional<double> scales_h,
    const c10::optional<double> scales_w) {
  api::Context* const context = api::context();

  TORCH_CHECK(
      (4 == input_arg.sizes().size()) && (2 == output_sizes.size()),
      "Invalid input!");

  const Tensor input = input_arg.is_vulkan() ? input_arg : input_arg.vulkan();
  const vTensor& v_input = convert(input);
  const auto v_input_sizes = v_input.sizes();

  vTensor v_output{
      context,
      {
          v_input_sizes[Layout::Activation4D::batch],
          v_input_sizes[Layout::Activation4D::channels],
          output_sizes[Layout::Parameter::height],
          output_sizes[Layout::Parameter::width],
      },
      input_arg.options(),
  };

  const struct Block final {
    uvec3 extents;
    uint32_t _;
    ivec2 iextents;
    vec2 scale;
  } block{
      v_output.extents(),
      0u,
      {
          safe_downcast<int32_t>(
              input_arg.size(Layout::Activation4D::width) - 1),
          safe_downcast<int32_t>(
              input_arg.size(Layout::Activation4D::height) - 1),
      },
      {
          compute_scales_value<float>(
              scales_w,
              v_input_sizes[Layout::Activation4D::width],
              output_sizes[Layout::Parameter::width]),
          compute_scales_value<float>(
              scales_h,
              v_input_sizes[Layout::Activation4D::height],
              output_sizes[Layout::Parameter::height]),
      },
  };

  api::UniformParamsBuffer params(context, block);
  api::PipelineBarrier pipeline_barrier{};

  context->submit_compute_job(
      // shader layout signature
      {
          VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
          VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
          VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER,
      },
      // shader descriptor
      VK_KERNEL(upsample_nearest2d),
      // pipeline barrier
      pipeline_barrier,
      // global work group size
      v_output.extents(),
      // local work group size
      adaptive_work_group_size(v_output.extents()),
      // fence handle
      VK_NULL_HANDLE,
      // shader arguments
      v_output.image(
          pipeline_barrier,
          api::PipelineStage::COMPUTE,
          api::MemoryAccessType::WRITE),
      v_input.image(pipeline_barrier, api::PipelineStage::COMPUTE),
      // params buffer
      params.buffer());

  return convert(v_output);
}

#ifdef USE_VULKAN_API

TORCH_LIBRARY_IMPL(aten, Vulkan, m) {
  m.impl(
      TORCH_SELECTIVE_NAME("aten::upsample_nearest2d"),
      TORCH_FN(upsample_nearest2d));
}

#endif /* USE_VULKAN_API */

} // namespace
} // namespace ops
} // namespace vulkan
} // namespace native
} // namespace at
