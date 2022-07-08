#pragma once

#include <torch/csrc/jit/frontend/function_schema_parser.h>
#include <unordered_set>

namespace torch {
namespace utils {

/**
 * class SchemaInfo
 *
 * Subclass of FunctionSchema that publicizes argument value specific operator
 * behavior (mutation, aliasing, special cases, etc...)
 */

struct TORCH_API SchemaInfo {
 public:
  explicit SchemaInfo(c10::FunctionSchema schema)
      : schema_(std::move(schema)) {}
  explicit SchemaInfo(const char* signature)
      : schema_(torch::jit::parseSchema(signature)) {}

  bool is_mutable();

  bool is_mutable(size_t index);

  bool is_mutable(c10::string_view name);

  void addArgumentValue(const std::string& name, const at::IValue& value);

  void addArgumentValues(
      const std::vector<c10::optional<at::IValue>>& value_list);

  void addArgumentValues(
      const std::unordered_map<std::string, at::IValue>& values);

 private:
  at::IValue flattenZeroDimIValue(const at::IValue& value) const;

  void generateAliasMaps();

  // Map of argument IValues
  std::unordered_map<std::string, at::IValue> value_map_;

  // Alias map of inputs with each other
  std::vector<std::unordered_set<size_t>> input_alias_map_;

  bool updated_;

  c10::FunctionSchema schema_;
};
} // namespace utils
} // namespace torch
