import torch
from torch.testing._internal.opinfo.core import (
    OpInfo,
)
from torch.testing._internal.common_dtype import all_types_and
import numpy as np
from torch._custom_op import CustomOp
from torch.testing._internal.autograd_function_db import (
    sample_inputs_numpy_cube,
    sample_inputs_numpy_mul,
    sample_inputs_numpy_sort,
    sample_inputs_numpy_take,
)

# Note: [custom op db]
#
# This is a collection of custom operator test cases written as OpInfos
# so they can easily be consumed by OpInfo-based tests to check if subsystems
# support them correctly.

def to_numpy(tensor):
    return tensor.cpu().numpy()

numpy_cube = CustomOp.define('_torch_testing::numpy_cube(Tensor x) -> (Tensor, Tensor)')

@numpy_cube.impl('cpu')
@numpy_cube.impl('cuda')
def numpy_cube_impl(x):
    x_np = to_numpy(x)
    dx = torch.tensor(3 * x_np ** 2, device=x.device)
    return torch.tensor(x_np ** 3, device=x.device), dx

@numpy_cube.impl_meta()
def numpy_cube_meta(x):
    return x.clone(), x.clone()

numpy_mul = CustomOp.define('_torch_testing::numpy_mul(Tensor x, Tensor y) -> Tensor')

@numpy_mul.impl('cpu')
@numpy_mul.impl('cuda')
def numpy_mul_impl(x, y):
    return torch.tensor(to_numpy(x) * to_numpy(y), device=x.device)

@numpy_mul.impl_meta()
def numpy_mul_meta(x, y):
    return x * y

numpy_sort = CustomOp.define('_torch_testing::numpy_sort(Tensor x, int dim) -> (Tensor, Tensor, Tensor)')

@numpy_sort.impl('cpu')
@numpy_sort.impl('cuda')
def numpy_sort_impl(x, dim):
    device = x.device
    x = to_numpy(x)
    ind = np.argsort(x, axis=dim)
    ind_inv = np.argsort(ind, axis=dim)
    result = np.take_along_axis(x, ind, axis=dim)
    return (
        torch.tensor(x, device=device),
        torch.tensor(ind, device=device),
        torch.tensor(ind_inv, device=device),
    )

@numpy_sort.impl_meta()
def numpy_sort_meta(x, dim):
    return x, x.long(), x.long()

numpy_take = CustomOp.define('_torch_testing::numpy_take(Tensor x, Tensor ind, Tensor ind_inv, int dim) -> Tensor')

@numpy_take.impl('cpu')
@numpy_take.impl('cuda')
def numpy_take_impl(x, ind, ind_inv, dim):
    device = x.device
    x = to_numpy(x)
    ind = to_numpy(ind)
    return torch.tensor(np.take_along_axis(x, ind, dim), device=device)

@numpy_take.impl_meta()
def numpy_take_meta(x, ind, ind_inv, dim):
    return x

custom_op_db = [
    OpInfo(
        'NumpyCubeCustomOp',
        op=numpy_cube,
        sample_inputs_func=sample_inputs_numpy_cube,
        dtypes=all_types_and(torch.bool, torch.half),
        supports_out=False,
    ),
    OpInfo(
        'NumpyMulCustomOp',
        op=numpy_mul,
        sample_inputs_func=sample_inputs_numpy_mul,
        dtypes=all_types_and(torch.bool, torch.half),
        supports_out=False,
    ),
    OpInfo(
        'NumpySortCustomOp',
        op=numpy_sort,
        sample_inputs_func=sample_inputs_numpy_sort,
        dtypes=all_types_and(torch.bool, torch.half),
        supports_out=False,
    ),
    OpInfo(
        'NumpyTakeCustomOp',
        op=numpy_take,
        sample_inputs_func=sample_inputs_numpy_take,
        dtypes=all_types_and(torch.bool, torch.half),
        supports_out=False,
    ),

]
