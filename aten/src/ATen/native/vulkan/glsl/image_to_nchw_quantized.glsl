#version 450 core
#define PRECISION $precision

layout(std430) buffer;

/* Qualifiers: layout - storage - precision - memory */

layout(set = 0, binding = 0) uniform PRECISION                    isampler3D uImage;
layout(set = 0, binding = 1) buffer  PRECISION                    Buffer {
  uint data[];
} uBuffer;
layout(set = 0, binding = 2) uniform PRECISION restrict           Block {
  ivec4 size;
  ivec4 offset;
} uBlock;

layout(local_size_x_id = 0, local_size_y_id = 1, local_size_z_id = 2) in;

void main() {
  const ivec3 pos = ivec3(gl_GlobalInvocationID);
  if (pos.y == 0 && pos.z == 0) {
      ivec4 texture_pos = ivec4(0,1,2,3) + 4 * pos.x;

      ivec4 last_eight;
      last_eight.z = texture_pos.x / (uBlock.size.x * uBlock.size.y);
      last_eight.w = texture_pos.x % (uBlock.size.x * uBlock.size.y);
      last_eight.y = last_eight.w / uBlock.size.x;
      last_eight.x = last_eight.w % uBlock.size.x;

      ivec4 sec_last_eight;
      sec_last_eight.z = texture_pos.y / (uBlock.size.x * uBlock.size.y);
      sec_last_eight.w = texture_pos.y % (uBlock.size.x * uBlock.size.y);
      sec_last_eight.y = sec_last_eight.w / uBlock.size.x;
      sec_last_eight.x = sec_last_eight.w % uBlock.size.x;

      ivec4 thr_last_eight;
      thr_last_eight.z = texture_pos.z / (uBlock.size.x * uBlock.size.y);
      thr_last_eight.w = texture_pos.z % (uBlock.size.x * uBlock.size.y);
      thr_last_eight.y = thr_last_eight.w / uBlock.size.x;
      thr_last_eight.x = thr_last_eight.w % uBlock.size.x;

      ivec4 four_last_eight;
      four_last_eight.z = texture_pos.w / (uBlock.size.x * uBlock.size.y);
      four_last_eight.w = texture_pos.w % (uBlock.size.x * uBlock.size.y);
      four_last_eight.y = four_last_eight.w / uBlock.size.x;
      four_last_eight.x = four_last_eight.w % uBlock.size.x;

      ivec3 last_eight_pos = ivec3(last_eight.x, last_eight.y, last_eight.z / 4);
      ivec3 sec_last_eight_pos = ivec3(sec_last_eight.x, sec_last_eight.y, sec_last_eight.z / 4);
      ivec3 thr_last_eight_pos = ivec3(thr_last_eight.x, thr_last_eight.y, thr_last_eight.z / 4);
      ivec3 four_last_eight_pos = ivec3(four_last_eight.x, four_last_eight.y, four_last_eight.z / 4);

      int texel_1 = texelFetch(uImage, last_eight_pos, 0)[last_eight.z];
      int texel_2 = texelFetch(uImage, sec_last_eight_pos, 0)[sec_last_eight.z];
      int texel_3 = texelFetch(uImage, thr_last_eight_pos, 0)[thr_last_eight.z];
      int texel_4 = texelFetch(uImage, four_last_eight_pos, 0)[four_last_eight.z];

      uint ui32 = (uint(texel_4 & 0xFF) << 24)
            | (uint(texel_3 & 0xFF) << 16)
            | (uint(texel_2 & 0xFF) << 8)
            | (uint(texel_1 & 0xFF));

      uBuffer.data[texture_pos.x / 4] = ui32;
  }
}
