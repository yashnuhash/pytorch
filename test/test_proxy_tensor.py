# Owner(s): ["oncall: fx"]

from torch.testing._internal.common_utils import TestCase, run_tests
import torch
import unittest
import warnings
from torch.testing._internal.common_device_type import instantiate_device_type_tests
from torch.testing._internal.common_methods_invocations import DecorateInfo
from torch.testing._internal.common_methods_invocations import op_db, wrapper_set_seed
from torch._subclasses.fake_tensor import DynamicOutputShapeException

from torch._decomp import decomposition_table
from torch.testing._internal.common_device_type import ops
from torch.fx.experimental.proxy_tensor import make_fx, DecompositionInterpreter
from torch.utils._pytree import tree_map

# Copied from functorch
def xfail(op_name, variant_name='', *, device_type=None, dtypes=None):
    return (op_name, variant_name, device_type, dtypes, True)


def skip(op_name, variant_name='', *, device_type=None, dtypes=None):
    return (op_name, variant_name, device_type, dtypes, False)


def skipOps(test_case_name, base_test_name, to_skip):
    all_opinfos = op_db
    for xfail in to_skip:
        op_name, variant_name, device_type, dtypes, expected_failure = xfail
        matching_opinfos = [o for o in all_opinfos
                            if o.name == op_name and o.variant_test_name == variant_name]
        assert len(matching_opinfos) >= 1, f"Couldn't find OpInfo for {xfail}"
        for opinfo in matching_opinfos:
            decorators = list(opinfo.decorators)
            if expected_failure:
                decorator = DecorateInfo(unittest.expectedFailure,
                                         test_case_name, base_test_name,
                                         device_type=device_type, dtypes=dtypes)
                decorators.append(decorator)
            else:
                decorator = DecorateInfo(unittest.skip("Skipped!"),
                                         test_case_name, base_test_name,
                                         device_type=device_type, dtypes=dtypes)
                decorators.append(decorator)
            opinfo.decorators = tuple(decorators)

    # This decorator doesn't modify fn in any way
    def wrapped(fn):
        return fn
    return wrapped


USE_TORCHVISION = False
try:
    import torchvision
    USE_TORCHVISION = True
except ImportError:
    warnings.warn("Couldn't import torchvision. Some of our tests use it, try "
                  "to install it with commands from pytorch.org, post-fixed with "
                  "`--no-deps` to avoid overwriting the pytorch installation",
                  UserWarning)


def _create_new_input(x):
    if not isinstance(x, torch.Tensor):
        return x
    if x.dtype != torch.float:
        return x + 1
    if x.is_leaf:
        return torch.rand_like(x, requires_grad=True)
    else:
        return torch.rand_like(x)

class TestProxyTensor(TestCase):
    def _test(self, f, inps):
        fx_f = make_fx(f)(*inps)
        new_inps = tree_map(_create_new_input, inps)
        self.assertEqual(fx_f(*new_inps), f(*new_inps))

    def test_make_fx_simple(self, device):
        def f(x):
            return torch.sin(x)
        self._test(f, (torch.randn(3),))

    def test_scalar_device(self, device):
        def f(a, b):
            return a + b
        self._test(f, [torch.randn(3, device=device), torch.tensor(5)])

    @unittest.skipIf(not USE_TORCHVISION, "test requires torchvision")
    def test_resnet18_backward_trace(self, device):
        mod = torchvision.models.resnet18()

        def f(x):
            for a in mod.parameters():
                a.grad = None
            out = mod(x)
            out.sum().backward()
            return [a.grad for a in mod.parameters()]

        inp = torch.randn(3, 3, 250, 250, requires_grad=True)
        self._test(f, [inp])

    def test_proxy_tensor(self):
        def f_grad(x):
            val = x.cos().cos().sum()
            return torch.autograd.grad(val, x)

        def f_backward(x):
            val = x.cos().cos().sum()
            val.backward()
            return x.grad

        for f in [f_grad, f_backward]:
            self._test(f, [torch.randn(3, requires_grad=True)])

    def test_inplace_metadata(self):
        def f(x):
            x = x.clone()
            x.unsqueeze_(-1)
            assert x.shape[-1] == 1
            return x

        self._test(f, [torch.randn(5)])

    def test_mode_tracing_factory_function(self):
        def f(x):
            return x + torch.randn(x.shape)

        # default behavior should trace factory functions
        traced = make_fx(f)(torch.randn(3))
        self.assertTrue(
            any(
                node.target == torch.ops.aten.randn.default
                for node in traced.graph.nodes
            )
        )

    def test_mode_tracing_factory_function_no_factory_function(self):
        def f(x):
            return x + torch.randn(x.shape)
        # setting the flag to false should not trace factory functions
        traced = make_fx(f, trace_factory_functions=False)(torch.randn(3))
        self.assertFalse(
            any(
                node.target == torch.ops.aten.randn.default
                for node in traced.graph.nodes
            )
        )

    def test_make_fx_overloads(self):
        def f(x):
            return x.cos() + torch.randn(x.shape)

        traced = make_fx(f)(torch.randn(3))

        self.assertTrue(all([isinstance(node.target, torch._ops.OpOverload)
                             for node in traced.graph.nodes if node.op == 'call_function']))

    def test_tensor_constants(self):
        def f():
            val = torch.tensor(float('inf'))
            return torch.full((100, 100), val)

        self._test(f, [])

    def test_constant_proxy_tensor(self):
        from torch.fx.experimental.proxy_tensor import make_fx

        def f():
            val = torch.tensor(float('inf'))
            return torch.full((100, 100), val)

        g = make_fx(f)()
        self.assertEqual(g(), f())

    def test_constant_proxy_tensor_mut(self):
        from torch.fx.experimental.proxy_tensor import make_fx

        def f():
            val = torch.tensor(float(1))
            val.add_(2)
            return torch.full((100, 100), val)

        g = make_fx(f)()
        self.assertEqual(g(), f())
        # In case we mutated shared state in the g graph!
        self.assertEqual(g(), f())

        g = make_fx(f, use_fake=True)()
        self.assertEqual(g(), f())
        # In case we mutated shared state in the g graph!
        self.assertEqual(g(), f())

    def test_use_fake_and_tensor(self):
        def f(x, y):
            z = torch.tensor([2.0, 3.0])
            return x + y + z

        g = make_fx(f, use_fake=True)(torch.randn(2), torch.randn(2))
        x, y = torch.randn(2), torch.randn(2)
        self.assertEqual(g(x, y), f(x, y))

    def test_decomposition_interpreter(self):
        def fn(x):
            return torch.nn.functional.silu(x)

        x = torch.rand((4, 4))
        fx_module = make_fx(fn, decomposition_table=None)(x)

        found_silu = False
        for n in fx_module.graph.nodes:
            if n.target == torch.ops.aten.silu or n.target == torch.ops.aten.silu.default:
                found_silu = True

        self.assertTrue(found_silu)

        new_graph = torch.fx.Graph()
        silu_decomp_table = {torch.ops.aten.silu.default: decomposition_table[torch.ops.aten.silu.default]}
        DecompositionInterpreter(
            fx_module,
            new_graph=new_graph,
            decomposition_table=silu_decomp_table,
        ).run(x)

        decomposed_module = torch.fx.GraphModule(fx_module, new_graph)

        for n in decomposed_module.graph.nodes:
            self.assertTrue(n.target != torch.ops.aten.silu)
            self.assertTrue(n.target != torch.ops.aten.silu.default)

        self.assertEqual(fx_module(x), decomposed_module(x))

make_fx_failures = {
    # unknown
    xfail('allclose'),
    xfail('equal'),
    xfail('linalg.eigvals'),
    xfail('nn.functional.max_pool1d', device_type='cpu'),
    # empty
    skip('new_empty'),
    skip('empty_like'),
    skip('empty'),
    # flaky
    skip('linalg.lstsq', 'grad_oriented'),
    skip('nn.functional.max_unpool1d', '', device_type='cpu'),
    skip('nn.functional.max_unpool2d', '', device_type='cpu'),
    skip('nn.functional.max_unpool3d', '', device_type='cpu'),
    skip('linalg.lstsq'),  # flaky, probably just a precision issue

    # data-dependent control flow
    xfail('cov'),
    xfail('istft'),
    xfail('nanquantile'),
    xfail('nn.functional.gaussian_nll_loss'),
    xfail('quantile'),
    xfail('tensor_split'),
    xfail('corrcoef'),

    # Seems like it's creating a sparse tensor that isn't captured by tensor.is_sparse
    xfail('sparse.sampled_addmm'),

    # ???
    xfail('nn.functional.ctc_loss'),
    # Sparse tensors are not supported with faketensors for now
    xfail('to_sparse'),
    # segfaults
    skip('block_diag'),
}

fake_tensor_failures = {
    # Needs complex-value support
    xfail('polar'),
    xfail('complex'),
    xfail('linalg.eig'),
    # FakeTensor fallback doesn't work
    xfail('linalg.matrix_power'),
    xfail('segment_reduce', 'lengths'),
    xfail('multinomial'),
    xfail('mvlgamma', 'mvlgamma_p_1'),
    xfail('mvlgamma', 'mvlgamma_p_3'),
    xfail('mvlgamma', 'mvlgamma_p_5'),
    xfail('cholesky'),
    xfail('cholesky_inverse'),
    # ASAN failures due to divide by 0
    skip('nn.functional.nll_loss'),
}


def _test_make_fx_helper(self, device, dtype, op, use_fake):
    def f(args, kwargs):
        return op.op(*args, **kwargs)
    sample_inputs_itr = op.sample_inputs(device, dtype, requires_grad=False)
    new_f = None
    for sample_input in sample_inputs_itr:
        args = [sample_input.input] + list(sample_input.args)
        kwargs = sample_input.kwargs

        try:
            new_f = make_fx(f, use_fake=use_fake)(args, kwargs)
        except DynamicOutputShapeException as e:
            self.skipTest("Dynamic output shape operation in trace")

        for arg in args:
            if isinstance(arg, torch.Tensor) and arg.dtype == torch.float:
                arg.uniform_(0, 1)
        try:
            old_out = f(args, kwargs)
        except Exception:
            continue
        new_out = wrapper_set_seed(new_f, args, kwargs)
        self.assertEqual(new_out, old_out)

class TestProxyTensorOpInfo(TestCase):
    @ops(op_db, allowed_dtypes=(torch.float,))
    @skipOps('TestProxyTensorOpInfo', 'test_make_fx_exhaustive', make_fx_failures)
    def test_make_fx_exhaustive(self, device, dtype, op):
        _test_make_fx_helper(self, device, dtype, op, False)

    @ops(op_db, allowed_dtypes=(torch.float,))
    @skipOps('TestProxyTensorOpInfo', 'test_make_fx_fake_exhaustive', make_fx_failures.union(fake_tensor_failures))
    def test_make_fx_fake_exhaustive(self, device, dtype, op):
        _test_make_fx_helper(self, device, dtype, op, True)



only_for = ("cpu")
instantiate_device_type_tests(
    TestProxyTensor,
    globals(),
    only_for=only_for,
)
instantiate_device_type_tests(TestProxyTensorOpInfo, globals(), only_for=only_for)


if __name__ == '__main__':
    run_tests()
