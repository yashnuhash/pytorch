#include <ATen/core/function_schema.h>

#include <iostream>
#include <stack>

namespace c10 {

void FunctionSchema::dump() const {
  std::cout << *this << "\n";
}

std::vector<Argument> FunctionSchema::getCorrectList(SchemaArgType type) const {
  if (type == SchemaArgType::input) {
    return arguments();
  } else {
    return returns();
  }
}

bool FunctionSchema::canAliasTypeSetsAlias(const c10::optional<std::vector<TypePtr>> &lhs, const c10::optional<std::vector<TypePtr>> &rhs) const {
  if (!lhs || !rhs) {
    return false;
  }
  for (const TypePtr& lhsType : *lhs) {
    for (const TypePtr& rhsType : *rhs) {
      if (lhsType == rhsType) {
        return true;
      }
    }
  }
  return false;
}

c10::optional<std::vector<TypePtr>> FunctionSchema::getAliasTypeSetContainedTypes(const c10::optional<std::vector<TypePtr>> &aliasTypeSet) const {
  if (!aliasTypeSet) {
    return c10::nullopt;
  }
  std::unordered_set<TypePtr> containedTypes;
  std::stack<TypePtr> typeStack;
  // Push all 1st level contained types into the stack.
  for (const TypePtr& type: *aliasTypeSet) {
    for (const TypePtr& containedType : type->containedTypes()){
      typeStack.push(containedType);
    }
  }

  // process all further level contained types.
  while (!typeStack.empty()) {
    TypePtr current = typeStack.top();
    typeStack.pop();
    if (!containedTypes.count(current)) {
      for (const TypePtr& containedType : current->containedTypes()) {
        typeStack.push(containedType);
      }
    }
    containedTypes.insert(current);
  }

  return std::vector<TypePtr>(containedTypes.begin(), containedTypes.end());
}

c10::optional<std::vector<TypePtr>> FunctionSchema::mapTypeToAliasTypeSet(const TypePtr& type) const {
  switch(type->kind()) {
    case TypeKind::ListType:
    case TypeKind::DictType:
    case TypeKind::ClassType:
    case TypeKind::TensorType:
      return std::vector<TypePtr> {c10::unshapedType(type)};
    case TypeKind::UnionType: {
      std::vector<TypePtr> mutable_types;
      for (const TypePtr& inner :
            type->expectRef<UnionType>().containedTypes()) {
        if (auto maybe_inner_types = mapTypeToAliasTypeSet(inner)) {
          mutable_types.insert(
              mutable_types.end(),
              (*maybe_inner_types).begin(),
              (*maybe_inner_types).end());
        }
      }
      if (mutable_types.size() == 0) {
        return c10::nullopt;
      }
      return mutable_types;
    }
    case TypeKind::AnyType:
      return {std::vector<TypePtr>{type}};
    case TypeKind::OptionalType: {
      auto inner = type->castRaw<OptionalType>()->getElementType();
      return mapTypeToAliasTypeSet(inner);
    }
    case TypeKind::TupleType: {
      std::vector<TypePtr> mutable_types;
      for (const TypePtr& inner : type->expectRef<TupleType>().elements()) {
        if (auto maybe_inner_types = mapTypeToAliasTypeSet(inner)) {
          mutable_types.insert(
              mutable_types.end(),
              (*maybe_inner_types).begin(),
              (*maybe_inner_types).end());
        }
      }
      if (mutable_types.size() == 0) {
        return c10::nullopt;
      }
      return {std::vector<TypePtr>{TupleType::create(mutable_types)}};
    }
    default:
      return c10::nullopt;
  }
}

bool FunctionSchema::may_alias(const SchemaArgument& lhs, const SchemaArgument& rhs) const {
  TORCH_INTERNAL_ASSERT(
      (lhs.index < getCorrectList(lhs.type).size()),
      "Invalid index for schema.");
  TORCH_INTERNAL_ASSERT(
      (rhs.index < getCorrectList(rhs.type).size()),
      "Invalid index for schema.");

  const Argument lhsArg = getCorrectList(lhs.type)[lhs.index];
  const Argument rhsArg = getCorrectList(rhs.type)[rhs.index];

  c10::optional<std::vector<TypePtr>> lhsTypes = mapTypeToAliasTypeSet(lhsArg.type());
  c10::optional<std::vector<TypePtr>> rhsTypes = mapTypeToAliasTypeSet(rhsArg.type());

  // Check to see if lhs and rhs have the same alias set
  if (canAliasTypeSetsAlias(lhsTypes, rhsTypes)) {
    if (lhsArg.alias_info() && rhsArg.alias_info()) {
      for (const auto& lhsSet : lhsArg.alias_info()->afterSets()) {
        for (const auto& rhsSet : rhsArg.alias_info()->afterSets()) {
          if (lhsSet == rhsSet) {
            return true;
          }
        }
      }
    }
  }

  return false;
}

bool FunctionSchema::may_contain_alias(const SchemaArgument& lhs, const SchemaArgument& rhs, bool bidirectional) const {
  bool may_alias_result = may_alias(lhs, rhs);
  if (may_alias_result) {
    return true;
  }

  const c10::Argument lhsArg = getCorrectList(lhs.type)[lhs.index];
  const c10::Argument rhsArg = getCorrectList(rhs.type)[rhs.index];
  c10::optional<std::vector<TypePtr>> lhsTypes = mapTypeToAliasTypeSet(lhsArg.type());
  c10::optional<std::vector<TypePtr>> rhsTypes = mapTypeToAliasTypeSet(rhsArg.type());
  c10::optional<std::vector<TypePtr>> lhsContainedTypes = getAliasTypeSetContainedTypes(lhsTypes);
  c10::optional<std::vector<TypePtr>> rhsContainedTypes = getAliasTypeSetContainedTypes(rhsTypes);

  // Checks if one side is wildcard and the other side is a container of the same type
  bool lhsWildcard = lhsArg.alias_info() && lhsArg.alias_info()->isWildcardAfter() && canAliasTypeSetsAlias(lhsTypes, rhsContainedTypes);
  bool rhsWildcard = rhsArg.alias_info() && rhsArg.alias_info()->isWildcardAfter() && canAliasTypeSetsAlias(rhsTypes, lhsContainedTypes);

  if (bidirectional) {
    return lhsWildcard || rhsWildcard || canAliasTypeSetsAlias(lhsContainedTypes, rhsContainedTypes);
  } else {
    return rhsWildcard || canAliasTypeSetsAlias(lhsContainedTypes, rhsContainedTypes);
  }
}
} // namespace c10
