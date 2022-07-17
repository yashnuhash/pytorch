# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
import functools
from typing import Any, Dict, Optional, Tuple, Callable, Union
import torch
from torch._C import _disabled_torch_function_impl
import torch.utils._pytree as pytree
from torch.fx import Tracer, GraphModule
from torch._subclasses.fake_tensor import FakeTensorMode
import torch.fx as fx
from torch.utils._mode_utils import no_dispatch
from torch.fx.passes.shape_prop import _extract_tensor_metadata
from contextlib import contextmanager, nullcontext

from torch.utils._python_dispatch import TorchDispatchMode, enable_torch_dispatch_mode

__all__ = ["ProxyTensor", "PythonKeyTracer", "dispatch_trace", "make_fx", "enable_strict", "DecompositionInterpreter"]
aten = torch.ops.aten

CURRENT_DECOMPOSITION_TABLE: Dict[torch._ops.OpOverload, Callable] = {}


@contextmanager
def decompose(decomposition_table):
    global CURRENT_DECOMPOSITION_TABLE
    old_decomposition_table = CURRENT_DECOMPOSITION_TABLE
    CURRENT_DECOMPOSITION_TABLE = decomposition_table
    try:
        yield CURRENT_DECOMPOSITION_TABLE
    finally:
        CURRENT_DECOMPOSITION_TABLE = old_decomposition_table

# Checks whether we try to convert the tensor into a scalar
IS_STRICT = True
def enable_strict(val):
    global IS_STRICT
    IS_STRICT = val

def wrap_output(inner_res, proxy_res, **kwargs):
    def wrap_with_proxy(e, proxy):
        if isinstance(e, torch.Tensor):
            with no_dispatch():
                return ProxyTensor(e, proxy, **kwargs)
        else:
            return e

    # Unfortunately, tree_map cannot directly be used here. As the resulting
    # object may be a proxy that represents a tuple, we may need to
    # explicitly unwrap the proxy by simulating the flattening operations.
    if isinstance(inner_res, tuple):
        return tuple(wrap_with_proxy(e, proxy_res[idx]) for idx, e in enumerate(inner_res))
    elif isinstance(inner_res, list):
        return list([wrap_with_proxy(e, proxy_res[idx]) for idx, e in enumerate(inner_res)])
    elif isinstance(inner_res, torch.Tensor):
        return wrap_with_proxy(inner_res, proxy_res)
    else:
        return inner_res


def maybe_disable_fake_tensor_mode():
    # TODO: figure out if this API generally makes sense and bake it into the
    # library
    mb_fake_mode = torch._C._get_torch_dispatch_mode()
    if isinstance(mb_fake_mode, FakeTensorMode):
        return enable_torch_dispatch_mode(mb_fake_mode.inner, replace=mb_fake_mode)
    else:
        return nullcontext()


def proxy_call(func_overload, args, kwargs=None):
    if kwargs is None:
        kwargs = {}
    func = func_overload.overloadpacket
    if func_overload in CURRENT_DECOMPOSITION_TABLE:
        return CURRENT_DECOMPOSITION_TABLE[func_overload](*args, **kwargs)
    if func_overload == aten._local_scalar_dense.default:
        t, = args
        assert not kwargs
        if t.constant is not None:
            with maybe_disable_fake_tensor_mode():
                return t.constant.item()
        raise RuntimeError("It appears that you're trying to get value out of a tracing tensor - erroring out! "
                           "It's likely that this is caused by data-dependent control flow or similar."
                           "Try torch.fx.experimental.proxy_tensor.enable_strict(False) to disable this check")

    def unwrap_proxy(e):
        return e.proxy if isinstance(e, ProxyTensor) else e

    def unwrap_elem(e):
        if isinstance(e, ProxyTensor):
            return e.elem
        return e

    proxy_args = pytree.tree_map(unwrap_proxy, args)
    proxy_kwargs = pytree.tree_map(unwrap_proxy, kwargs)

    proxy_res = func_overload(*proxy_args, **proxy_kwargs)
    # Kind of a hacky way to test if an op is in-place or not
    if func.__name__[-1] == "_" and func.__name__[0] != "_":
        args[0].proxy = proxy_res
        proxy_res.node.meta['tensor_meta'] = _extract_tensor_metadata(args[0])
    inner_res = func_overload(*pytree.tree_map(unwrap_elem, args), **pytree.tree_map(unwrap_elem, kwargs))

    # Needed to sync up metadata for in-place operators that modify metadata
    # TODO: instead forward the metadata to the inner tensor so updating
    # is not necessary
    if torch.Tag.inplace_view in func_overload.tags:  # type: ignore[attr-defined]
        with no_dispatch():
            func_overload(*args, **kwargs)

    # In some circumstances, we will be tracing in a situation where a tensor
    # is *statically* known to be a constant (currently, this only happens if
    # you run torch.tensor; deterministic factory functions like torch.arange
    # don't get this treatment).  When the tensor in question is small, it's
    # helpful to due constant propagation in case we call item() (in which
    # case we can return the constant value that is known, rather than give
    # an error.)  The logic here tests if constant propagation is possible
    # (because all of the inputs are constant).  If so, we disable fake tensor
    # mode (if it is on) and do true compute on the constant.
    #
    # It's worth highlighting that we're making a policy decision here.
    # There is a potential that the tensor is actually quite large, and we
    # don't actually want to run the compute.  The tensor being quite large
    # is one of the reasons why factory functions don't get this treatment
    # (since they can be quite large; if a parameter is initialized to a
    # constant value it will be!)  Similarly, there is also a potential
    # to run an operator that blows up the size of a small tensor; we don't
    # protect against this case, but we could force, e.g., only single
    # element constant computation by testing the numel of the result before
    # propagating const-ness.  Similarly, we don't require the constant to
    # live on CPU, but we could.
    all_constant = True
    any_constant = False

    def check_constant(e):
        nonlocal all_constant, any_constant
        if isinstance(e, ProxyTensor):
            if e.constant is None:
                all_constant = False
            else:
                any_constant = True

    pytree.tree_map(check_constant, args)
    pytree.tree_map(check_constant, kwargs)

    def unwrap_constant(e):
        if isinstance(e, ProxyTensor):
            return e.constant
        return e

    constant = None
    # NB: do NOT include factories as constants
    if all_constant and any_constant:
        with maybe_disable_fake_tensor_mode():
            constant = func_overload(
                *pytree.tree_map(unwrap_constant, args),
                **pytree.tree_map(unwrap_constant, kwargs)
            )

    # TODO(chilli): Enable this after it's been refactored to work with wrapper tensor subclasses in general
    # pytree.tree_map(lambda x: check_metadata_consistency(x, ProxyTensor), (inner_res, args, kwargs))
    return wrap_output(inner_res, proxy_res, constant=constant)


class ProxyTensor(torch.Tensor):
    proxy: fx.Proxy
    elem: torch.Tensor


    @staticmethod
    def __new__(cls, elem, proxy, *, requires_grad=None, constant=None):
        r = torch.Tensor._make_wrapper_subclass(  # type: ignore[attr-defined]
            cls,
            elem.shape, dtype=elem.dtype, layout=elem.layout, device=elem.device,
            requires_grad=requires_grad if requires_grad is not None else False, strides=elem.stride(),
            storage_offset=elem.storage_offset()
        )
        return r

    def __init__(self, elem, proxy, *, requires_grad=None, constant=None):
        if elem.is_sparse:
            proxy.node.meta['tensor_meta'] = {}
        else:
            proxy.node.meta['tensor_meta'] = _extract_tensor_metadata(self)
        # This detects situations where you accidentally put a ProxyTensor
        # inside a ProxyTensor for the same trace; this is a layering violation
        assert not (isinstance(elem, ProxyTensor) and elem.proxy.tracer is proxy.tracer)
        self.elem = elem
        self.proxy = proxy
        self.constant = constant

    def __deepcopy__(self, memo):
        return self.clone()

    def __repr__(self):
        with no_dispatch():
            return f"ProxyTensor({self.elem}, proxy={self.proxy})"

    __torch_function__ = _disabled_torch_function_impl

    @classmethod
    def __torch_dispatch__(cls, func_overload, types, args=(), kwargs=None):
        return proxy_call(func_overload, args, kwargs)


class PythonKeyTracer(Tracer):
    def __init__(self):
        super().__init__()

    # In general, we don't want to make modules leaves. In principle, users of
    # this tracer might want to override this in order to turn a couple specific
    # modules into leaves in the traced graph.
    def call_module(
            self, m: torch.nn.Module, forward: Callable[..., Any], args: Tuple[Any, ...], kwargs: Dict[str, Any]
    ) -> Any:
        return forward(*args, **kwargs)

    def create_arg(self, a: Any):
        if isinstance(a, torch.nn.Parameter):
            for n, p in self.root.named_parameters():
                if a is p:
                    return self.create_node('get_attr', n, (), {})
            qualname: Optional[str] = None

            if not qualname:
                i = 0
                while True:
                    qualname = f'_param_constant{i}'
                    if not hasattr(self.root, qualname):
                        break
                    i += 1
                setattr(self.root, qualname, a)

            return self.create_node('get_attr', qualname, (), {})
        return super().create_arg(a)


def dispatch_trace(
        root: Union[torch.nn.Module, Callable],
        tracer: Tracer,
        concrete_args: Optional[Tuple[Any, ...]] = None,
) -> GraphModule:
    graph = tracer.trace(root, concrete_args)
    name = root.__class__.__name__ if isinstance(root, torch.nn.Module) else root.__name__
    return GraphModule(tracer.root, graph, name)


def wrap_key(f, inps):
    flat_inps, _ = pytree.tree_flatten(inps)

    @functools.wraps(f)
    def wrapped(*args):
        flat_args, args_spec = pytree.tree_flatten(args)
        assert (len(flat_args) == len(flat_inps))
        for idx, arg in enumerate(flat_args):
            if isinstance(flat_inps[idx], torch.Tensor):
                with no_dispatch():
                    flat_args[idx] = ProxyTensor(
                        flat_inps[idx],
                        arg,
                        requires_grad=(flat_inps[idx].is_leaf and flat_inps[idx].requires_grad)
                    )
            else:
                flat_args[idx] = flat_inps[idx]

        tree_args = pytree.tree_unflatten(flat_args, args_spec)
        out = f(*tree_args)
        flat_outs, out_spec = pytree.tree_flatten(out)
        for idx in range(len(flat_outs)):
            if isinstance(flat_outs[idx], torch.Tensor) and isinstance(flat_outs[idx], ProxyTensor):
                flat_outs[idx] = flat_outs[idx].proxy
        return pytree.tree_unflatten(flat_outs, out_spec)

    return wrapped


class ProxyTorchDispatchMode(TorchDispatchMode):
    def __init__(self, tracer):
        self.tracer = tracer

    def __torch_dispatch__(self, func_overload, types, args=(), kwargs=None):
        func = func_overload.overloadpacket
        # We don't want to convert torch.tensor constants into tracing objects.
        if func_overload == aten.lift.default:
            return args[0]
        if any(tuple(isinstance(arg, ProxyTensor) for arg in pytree.tree_flatten(args)[0])):
            return proxy_call(func_overload, args, kwargs)
        # When we trace through a torch.tensor invocation, you never actually
        # see a torch.ops.aten.tensor call. Instead, the way this function is
        # implemented internally is that we allocate a plain tensor (this is
        # *guaranteed* to be a plain tensor, we disable all modes when doing
        # so), and then call at::lift_fresh on it (to give modes a chance to do
        # their stuff).  Furthermore, the tensor argument to lift_fresh is guaranteed
        # to be freshly allocated, so we want lift_fresh to be a no-op (directly
        # returning the input argument).
        #
        # Here is the basic problem: when we trace this sequence of executions
        # into an FX graph, what happens to this call sequence?  Traditionally,
        # tensor constants get interned as buffers on the FX GraphModule.  But
        # this is dangerous.  Consider:
        #
        #       x = torch.tensor(1)
        #       x.add_(2)
        #
        # Naively, this traces into:
        #
        #       t = self._tensor_constant0  # initialized to torch.tensor(1)
        #       x = torch.ops.aten.lift_fresh(t)
        #       x.add_(2)
        #
        # If lift_fresh returns t directly, the subsequent add_ call will
        # modify the tensor constant. Really, the problem is we've violated
        # the invariant the the argument to lift is fresh.  So what we should
        # preserve the invariant by replacing lift_fresh with lift_fresh_copy:
        #
        #       t = self._tensor_constant0  # initialized to torch.tensor(1)
        #       x = torch.ops.aten.lift_fresh_copy(t)
        #       x.add_(2)
        #
        # This is what the overload modification does.
        else:
            if func_overload is torch.ops.aten.lift_fresh.default:
                func_overload = torch.ops.aten.lift_fresh_copy.default

            proxy_res = self.tracer.create_proxy('call_function', func_overload, args, kwargs,
                                                 name=self.tracer.graph._target_to_str(func.__name__))

            inner_res = func_overload(*args, **kwargs)

            # If this is a lift, the input tensor is guaranteed to be a
            # constant, so we keep a copy of the original argument along so
            # we can query it if we're asked to item() it at some later point
            is_lift = func_overload is torch.ops.aten.lift_fresh_copy.default
            if is_lift:
                with maybe_disable_fake_tensor_mode():
                    constant = args[0].clone()
            else:
                constant = None
            return wrap_output(inner_res, proxy_res, constant=constant)


class DecompositionInterpreter(torch.fx.Interpreter):
    def __init__(self, module: torch.fx.GraphModule, new_graph: torch.fx.Graph, decomposition_table=None, **kwargs):
        super().__init__(module, **kwargs)
        self.new_graph = new_graph
        self.tracer = torch.fx.proxy.GraphAppendingTracer(self.new_graph)
        self.decomposition_table = decomposition_table
        if self.decomposition_table is None:
            self.decomposition_table = {}

    def placeholder(self, target, args, kwargs):
        out = super().placeholder(target, args, kwargs)
        # TODO handle case where the first character of target is '*'
        return ProxyTensor(out, torch.fx.Proxy(self.new_graph.placeholder(target), self.tracer))

    def get_attr(self, target, args, kwargs):
        out = super().get_attr(target, args, kwargs)
        return ProxyTensor(out, torch.fx.Proxy(self.new_graph.get_attr(target), self.tracer))

    # call_function, call_method, call_module get traced automatically by the ProxyTensors.

    def output(self, target, args, kwargs):
        out = super().output(target, args, kwargs)

        def unwrap(e):
            return e.proxy.node if isinstance(e, ProxyTensor) else e
        self.new_graph.output(pytree.tree_map(unwrap, out))
        return out

    def run(self, *args, **kwargs):
        with decompose(self.decomposition_table):
            return super().run(*args, **kwargs)

def make_fx(f, decomposition_table=None, trace_factory_functions=True, use_fake=False):
    if use_fake and not trace_factory_functions:
        raise ValueError("""\
use_fake and not trace_factory_functions is not currently supported; if
proxy tensor is not executed as a mode, fake tensors must not be executed
as a mode either (otherwise, we will incorrectly intern fake tensors into
the traced graph module.)  However, non-mode execution of fake tensors
is not currently supported (although, in principle, it could be; file
a bug if you need this)""")

    if decomposition_table is None:
        decomposition_table = {}

    @functools.wraps(f)
    def wrapped(*args):
        phs = pytree.tree_map(lambda _: fx.PH, args)  # type: ignore[attr-defined]
        fx_tracer = PythonKeyTracer()
        fake_tensor_mode = FakeTensorMode() if use_fake else nullcontext()
        proxy_mode = ProxyTorchDispatchMode(fx_tracer) if trace_factory_functions else nullcontext()

        def wrap_fake(x):
            if isinstance(x, torch.Tensor):
                return fake_tensor_mode.from_tensor(x)  # type: ignore[attr-defined]

            return x

        if use_fake:  # type: ignore[attr-defined]
            args = pytree.tree_map(wrap_fake, args)

        with decompose(decomposition_table), fake_tensor_mode, proxy_mode:  # type: ignore[attr-defined]
            t = dispatch_trace(wrap_key(f, args), tracer=fx_tracer, concrete_args=tuple(phs))
        return t

    return wrapped
