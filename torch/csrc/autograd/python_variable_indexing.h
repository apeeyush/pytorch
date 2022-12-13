#pragma once

#include <Exceptions.h>
#include <c10/core/SymInt.h>
#include <pyport.h>
#include <sys/types.h>
#include <torch/csrc/autograd/python_variable.h>
#include <torch/csrc/python_headers.h>
#include <utils/python_symnode.h>

namespace torch {
namespace autograd {

struct UnpackedSlice {
  c10::SymInt start;
  c10::SymInt stop;
  c10::SymInt step;
};

// This mirrors Cpython's PySlice_Unpack method
inline UnpackedSlice __PySlice_Unpack(PyObject* _r) {
  PySliceObject* r = (PySliceObject*)_r;
  /* this is harder to get right than you might think */

  // Py_BUILD_ASSERT replaced because it is not available in all versions
  static_assert(PY_SSIZE_T_MIN + 1 <= -PY_SSIZE_T_MAX, "Build failed");

  c10::SymInt start_sym, stop_sym, step_sym;

  if (r->step == Py_None) {
    step_sym = c10::SymInt(1);
  } else {
    TORCH_CHECK(
        py::isinstance(r->step, torch::get_symint_class()),
        "Slicing step can't be symint")
    // NOLINTNEXTLINE(cppcoreguidelines-init-variables)
    Py_ssize_t step;
    bool result = _PyEval_SliceIndex(r->step, &step);
    TORCH_CHECK(!result, "Failed parsing slicing step to integer")
    TORCH_CHECK(step != 0, "Slicing step size can't be zero");

    /* Here *step might be -PY_SSIZE_T_MAX-1; in this case we replace it
     * with -PY_SSIZE_T_MAX.  This doesn't affect the semantics, and it
     * guards against later undefined behaviour resulting from code that
     * does "step = -step" as part of a slice reversal.
     */
    if (step < -PY_SSIZE_T_MAX) {
      step = -PY_SSIZE_T_MAX;
    }
    step_sym = c10::SymInt(step);
  }

  if (py::isinstance(r->start, torch::get_symint_class())) {
    auto new_obj = py::reinterpret_borrow<py::object>(r->start);
    start_sym = new_obj.cast<c10::SymInt>();
  } else if (r->start == Py_None) {
    TORCH_CHECK(
        !step_sym.is_symbolic(),
        "Can't use symbolic step size to determine slicing start index")
    start_sym = c10::SymInt(step_sym.expect_int() < 0 ? PY_SSIZE_T_MAX : 0);
  } else {
    // NOLINTNEXTLINE(cppcoreguidelines-init-variables)
    Py_ssize_t start;
    bool result = _PyEval_SliceIndex(r->start, &start);
    TORCH_CHECK(result, "Failed parsing slicing start index to integer")
    start_sym = c10::SymInt(start);
  }

  if (py::isinstance(r->stop, torch::get_symint_class())) {
    auto new_obj = py::reinterpret_borrow<py::object>(r->stop);
    stop_sym = new_obj.cast<c10::SymInt>();
  } else if (r->stop == Py_None) {
    TORCH_CHECK(
        !step_sym.is_symbolic(),
        "Can't use symbolic step size to determine slicing stop index")
    stop_sym = c10::SymInt(
        step_sym.expect_int() < 0 ? PY_SSIZE_T_MIN : PY_SSIZE_T_MAX);
  } else {
    // NOLINTNEXTLINE(cppcoreguidelines-init-variables)
    Py_ssize_t stop;
    bool result = _PyEval_SliceIndex(r->stop, &stop);
    TORCH_CHECK(result, "Failed parsing slicing stop index to integer")
    stop_sym = c10::SymInt(stop);
  }

  return UnpackedSlice{start_sym, stop_sym, step_sym};
}

Py_ssize_t THPVariable_length(PyObject* self);
PyObject* THPVariable_getitem(PyObject* self, PyObject* index);
int THPVariable_setitem(PyObject* self, PyObject* index, PyObject* value);

Variable valueToTensor(
    c10::TensorOptions options,
    PyObject* value,
    const at::Device& device);

} // namespace autograd
} // namespace torch
