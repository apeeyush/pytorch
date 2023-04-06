# Owner(s): ["module: onnx"]
from __future__ import annotations

import io
import os
import tempfile
import unittest

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import onnx_test_common
import onnxruntime  # type: ignore[import]
import torch
import torch.onnx
import transformers  # type: ignore[import]
from torch import nn

from torch._subclasses import fake_tensor
from torch.onnx._internal import _beartype, diagnostics, fx as fx_onnx
from torch.onnx._internal.fx.dynamo_exporter import DynamoOptimizeExporter
from torch.onnx._internal.fx.fx_symbolic_exporter import FXSymbolicTraceExporter
from torch.testing._internal import common_utils
from torch.types import Number

_NumericType = Union[Number, torch.Tensor, np.ndarray]
_ModelType = Union[torch.nn.Module, Callable]
_InputArgsType = Optional[Union[torch.Tensor, Sequence[Any], Mapping[str, Any]]]
_OutputsType = Sequence[_NumericType]


@_beartype.beartype
def _run_ort(
    onnx_model: Union[str, torch.onnx.ExportOutput],
    pytorch_inputs: Sequence[_InputArgsType],
) -> _OutputsType:
    if isinstance(onnx_model, torch.onnx.ExportOutput):
        buffer = io.BytesIO()
        onnx_model.save(buffer)
        ort_model = buffer.getvalue()
    else:
        ort_model = onnx_model
    session = onnxruntime.InferenceSession(
        ort_model, providers=["CPUExecutionProvider"]
    )
    input_names = [ort_input.name for ort_input in session.get_inputs()]
    if len(input_names) != len(pytorch_inputs):
        raise AssertionError(
            f"Expected {len(input_names)} inputs, got {len(pytorch_inputs)}"
        )
    return session.run(
        None, {k: v.cpu().numpy() for k, v in zip(input_names, pytorch_inputs)}
    )


@_beartype.beartype
def _validate_export_output(
    export_output: torch.onnx.ExportOutput,
    model: _ModelType,
    input_args: Sequence[_InputArgsType],
    input_kwargs: Mapping[str, _InputArgsType],
    atol: float,
    rtol: float,
):
    # Format original model inputs into the format expected by exported ONNX model.
    onnx_format_args = export_output.input_formatter.to_onnx(
        *input_args, **input_kwargs
    )

    ref_outputs = export_output.output_formatter.to_onnx(
        model(*input_args, **input_kwargs)
    )
    ort_outputs = _run_ort(export_output, onnx_format_args)
    if len(ref_outputs) != len(ort_outputs):
        raise AssertionError(
            f"Expected {len(ref_outputs)} outputs, got {len(ort_outputs)}"
        )
    for ref_output, ort_output in zip(ref_outputs, ort_outputs):
        torch.testing.assert_close(
            ref_output, torch.tensor(ort_output), rtol=rtol, atol=atol
        )


@_beartype.beartype
def _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(
    model: _ModelType,
    input_args: Sequence[_InputArgsType],
    rtol: float = 1e-3,
    atol: float = 1e-7,
    opset_version: int = 18,
    dynamic_shapes: bool = True,
    **input_kwargs,
):
    # Feed args and kwargs into exporter.
    # Note that exporter should flatten kwargs into positional args the exported model;
    # since ONNX doesn't represent kwargs.
    export_output = torch.onnx.dynamo_export(
        model,
        *input_args,
        **input_kwargs,
        export_options=torch.onnx.ExportOptions(
            opset_version=opset_version, dynamic_shapes=dynamic_shapes
        ),
    )

    _validate_export_output(export_output, model, input_args, input_kwargs, atol, rtol)

    # NOTE: Temporarily run `DynamoOptimizeExporter` here as well to ensure coverage.
    # Remove after `DynamoOptimizeExporter` is removed. Or refactor with parameterization.

    export_output = DynamoOptimizeExporter(
        torch.onnx.ExportOptions(
            opset_version=opset_version, dynamic_shapes=dynamic_shapes
        ),
        model=model,
        model_args=input_args,
        model_kwargs=input_kwargs,
    ).export()

    _validate_export_output(export_output, model, input_args, input_kwargs, atol, rtol)


class TestFxToOnnxWithOnnxRuntime(onnx_test_common._TestONNXRuntime):
    def setUp(self):
        super().setUp()
        self.diag_ctx = diagnostics.engine.create_diagnostic_context(
            "test_fx_export", version=torch.__version__
        )
        self.opset_version = 18

    def tearDown(self):
        diagnostics.engine.dump(
            f"test_report_{self._testMethodName}.sarif", compress=False
        )
        super().tearDown()

    def test_simple_function(self):
        def func(x):
            # TODO(justinchuby): Replicate torch's type casting policy
            # in the exporter for type promotion support
            y = x + 1.0
            z = y.relu()
            return (y, z)

        tensor_x = torch.randn(1, 1, 2, dtype=torch.float32)

        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(func, (tensor_x,))

    # AssertionError: Dynamo input/output is not consistent with traced input/output
    # https://github.com/pytorch/pytorch/issues/96379
    # TODO: `DynamoOptimizeExporter` works for this test case. Re-enable for that
    # after parameterization.
    @unittest.expectedFailure
    def test_func_with_args_and_tensor_kwargs(self):
        # Non-tensor optional kwargs are always folded into constant and
        # removed from input list in Dynamo-traced graph, if its value is not provided
        # to tracer. So for a function like
        #   def func(x, b=1.0)
        # here. E.g., if you first Dynamo-trace the model with arguments (x,),
        # and then call the traced graph with arguments (x, b=2.0), it will complain
        # somewhere that model is called with extra args because the modified
        # function is traced into
        #   def forward(self, x : torch.Tensor):
        #     add = x + 1.0;  x = None
        #     relu = add.relu()
        #     return (add, relu)
        # To summarize, in order to be traced as graph input, the value of optional kwarg
        # must be provided. Otherwise, they are treated as in-graph constants in Dynamo.
        # Tensor optional kwargs are an exception. It is always traced as input.
        # It is unclear if this behavior is intended or not. But in general it is bad
        # practice to set mutable default values.
        # `DynamoOptimizeExporter` applies a workaround by binding args and kwargs to
        # model signature and fill in the default values of unprovided optional arguments.
        def func(x, b=torch.tensor(1.0)):
            y = x + b
            z = y.relu()
            return (y, z)

        tensor_x = torch.randn(1, 1, 2, dtype=torch.float32)

        # Test without providing optional kwarg.
        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(func, (tensor_x,))
        # Test with only positional args.
        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(
            func, (tensor_x, torch.tensor(8.0))
        )
        # Test while specifying optional kwarg.
        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(
            func, (tensor_x,), b=torch.tensor(5.0)
        )

    # beartype.roar.BeartypeCallHintParamViolation:
    # @beartyped onnxscript.function_libs.torch_aten.graph_building.TorchScriptGraph.add_input()
    # parameter input_value=8.0 violates type hint typing.Union[torch.Tensor, NoneType],
    # as float 8.0 not <class "builtins.NoneType"> or <protocol "torch.Tensor">.
    @unittest.expectedFailure
    def test_func_with_args_and_kwargs(self):
        def func(x, b=1.0):
            y = x + b
            z = y.relu()
            return (y, z)

        tensor_x = torch.randn(1, 1, 2, dtype=torch.float32)

        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(func, (tensor_x,))
        # Test with only positional args.
        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(func, (tensor_x, 8.0))
        # Test while specifying optional kwarg.
        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(func, (tensor_x,), b=5.0)

    def test_func_with_nested_input_structure(self):
        def func(
            x_dict: Dict[str, torch.Tensor],
            y_tuple: Tuple[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]],
            z_list: List[List[torch.Tensor]],
        ):
            if "a" in x_dict:
                x = x_dict["a"]
            elif "b" in x_dict:
                x = x_dict["b"]
            else:
                x = torch.randn(3)

            y1, (y2, y3) = y_tuple

            z = x + y1 + y2 + y3
            for z_sub_list in z_list:
                z = z + torch.stack(z_sub_list).sum()

            return z

        # NOTE: `DynamoOptimizeExporter` fails if used argument 'c' is passed in.
        x_dict = {"a": torch.randn(3)}  # , "c": torch.randn(3)}
        y_tuple = (torch.randn(3), (torch.randn(3), torch.randn(3)))
        z_list = [
            [torch.randn(3), torch.randn(3)],
            [torch.randn(3), torch.randn(3), torch.randn(3)],
        ]
        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(
            func, (x_dict, y_tuple, z_list)
        )

    def test_func_with_nested_output_structure(self):
        def func(x, y, z):
            x = x + y
            y = y + z
            z = x + y
            out1 = (x, (y, z))
            out2 = [[x, y], [y, z]]
            out3 = {"z": z, "x": x}
            return out1  # , out2, out3

        x = torch.randn(3)
        y = torch.randn(3)
        z = torch.randn(3)
        # NOTE: `DynamoOptimizeExporter` fails if `, out2, out3` is uncommented and returned.
        # It does not capture the output structure, which is the non computation part of
        # the graph. It only sets `(x, y, z)` as output.
        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(func, (x, y, z))

    @unittest.skip("ORT segfaults")
    def test_mnist(self):
        class MNISTModel(nn.Module):
            def __init__(self):
                super().__init__()
                self.conv1 = nn.Conv2d(1, 32, 3, 1, bias=True)
                self.conv2 = nn.Conv2d(32, 64, 3, 2, bias=True)
                self.fc1 = nn.Linear(9216, 128, bias=True)
                self.fc2 = nn.Linear(128, 10, bias=True)

            def forward(self, tensor_x: torch.Tensor):
                tensor_x = self.conv1(tensor_x)
                tensor_x = torch.sigmoid(tensor_x)
                tensor_x = self.conv2(tensor_x)
                tensor_x = torch.sigmoid(tensor_x)
                tensor_x = torch.flatten(tensor_x, 1)
                tensor_x = self.fc1(tensor_x)
                tensor_x = torch.sigmoid(tensor_x)
                output = self.fc2(tensor_x)
                return output

        tensor_x = torch.rand((64, 1, 28, 28), dtype=torch.float32)
        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(MNISTModel(), (tensor_x,))

    # test single op with no kwargs
    def test_sigmoid(self):
        x = torch.randn(1, 4, 2, 3)

        class SigmoidModel(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.sigmoid = torch.nn.Sigmoid()

            def forward(self, x):
                return self.sigmoid(x)

        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(SigmoidModel(), (x,))

    # test single op with no kwargs
    def test_sigmoid_add(self):
        # TODO(titaiwang): change to randn once it's ready
        x = torch.tensor([1.0, 2.0], dtype=torch.float)

        class SigmoidAddModel(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.sigmoid = torch.nn.Sigmoid()

            def forward(self, x):
                x = torch.ops.aten.add(x, 1.0, alpha=2.0)
                return self.sigmoid(x)

        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(SigmoidAddModel(), (x,))

    def test_none_input(self):
        class NoneInputModel(torch.nn.Module):
            def forward(
                self, x: torch.Tensor, y: Optional[torch.Tensor], z: torch.Tensor
            ):
                if y is None:
                    return x + z
                return x + y + z

        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(
            NoneInputModel(), (torch.randn(1, 2), None, torch.randn(1, 2))
        )

    def test_gpt2_tiny(self):
        model_name = "sshleifer/tiny-gpt2"
        # Download pytorch model
        model = transformers.AutoModel.from_pretrained(model_name)
        tokenizer = transformers.AutoTokenizer.from_pretrained(model_name)

        # Transform input tokens
        inputs = tokenizer("Hello world!", return_tensors="pt")

        # FIXME(titaiwang): SegFault when symbolic tracing is used
        # https://github.com/microsoft/onnx-script/issues/523
        _run_test_with_fx_to_onnx_exporter_and_onnx_runtime(
            model, [], **inputs, dynamic_shapes=False
        )

    @_beartype.beartype
    def _test_large_scale_exporter(
        self,
        model_name: str,
        create_model: Callable,
        create_args: Callable,
        create_pytorch_only_kwargs: Callable,
        enable_dynamic_axes: bool = True,
    ):
        """Test helper for large-scale exporter.

        Arguments:
            model_name: Name of the model. It used to name temporary files.
            create_model: A function that creates a model. It should always create the same model.
            create_args: A function that creates random input arguments for the model.
            create_pytorch_only_kwargs: A function that creates kwargs for calling PyTorch model with real tensors.
            enable_dynamic_axes: Whether to export the model with dynamic axes. This would set
                the shape of input and nodes all to dynamic by following symbolic fx graph.
                op_level_debug is not supported when dynamic axes is on.

        This test contains several steps.

        1. Create a toy model.
        2. Save the toy's state (parameters) to a file. This is for simulating a checkpoint file.
        3. Load it back and export it to ONNX with large-scale exporter.
            All operations (including model loading) are done under
            FakeTensorMode so no real tensor is created and no real
            computation happens.
        4. The ONNX model generated in step 3 doesn't contain parameters,
            and this step adds them as external data and save a new ONNX model.
        5. Run PyTorch and ONNX models and compare their results.
        """

        # Create the toy model.
        model = create_model()

        with tempfile.NamedTemporaryFile(
            prefix=model_name, suffix=".pt"
        ) as tmp_file, tempfile.TemporaryDirectory(
            suffix="large_scale_export"
        ) as tmp_folder:
            # Dump state_dict to a file to simulate how HuggingFace model is initialized.
            # The file will be loaded via .load_state_dict(...)
            torch.save(model.state_dict(), tmp_file.name)

            ftm = fake_tensor.FakeTensorMode(
                allow_non_fake_inputs=True, allow_fallback_kernels=False
            )
            ctx = fx_onnx.FxToOnnxContext()
            # NOTE: FakeTensorMode disallows symbolic shape of fx graph
            # The following coed block does several things.
            #  1. Create a model whose parameters and buffers are all FakeTensor's.
            #  2. Convert nn.Module into ONNX model without initializers.
            #  3. Record the file paths to find real initializers.
            with ctx, ftm:
                # Toy model with parameters and buffers as FakeTensor's.
                fake_model = create_model()
                fake_model.load_state_dict(torch.load(tmp_file.name))
                # Toy inputs as FakeTensor's.
                fake_args = create_args()
                # Export ONNX model without initializers while ctx.paths records
                # all files that contains real initializers.

                export_output = FXSymbolicTraceExporter(
                    options=torch.onnx.ExportOptions(
                        opset_version=self.opset_version,
                        dynamic_shapes=enable_dynamic_axes,
                    ),
                    model=fake_model,
                    model_args=fake_args,
                    model_kwargs={},
                ).export()

                onnx_model = export_output.model_proto

            # Tasks done by the following block.
            #  1. Iterate through all tensors stored in ctx.paths (the file content is loaded torch.load)
            #  2. If a tensor's name matches a "onnx_model"'s input name, an initializer is created and saved to
            #     a seperated folder.
            #  3. A new ONNX model is saved into file with the initializers saved in the previous step.
            #  4. ORT executes the new ONNX model and compares the results with the original GPT model.

            # Model saved to tmp_folder/onnx_model_location
            # Initializers are saved to tmp_folder/onnx_initializer_location/*.onnx
            onnx_model_location = model_name + "_external_data.onnx"
            onnx_initializer_location = model_name + "_initializers"
            fx_onnx.save_model_with_external_data(
                tmp_folder,
                onnx_model_location,
                onnx_initializer_location,
                tuple(ctx.paths),
                onnx_model,
            )

            # Generate random inputs.
            args = create_args()
            kwargs = create_pytorch_only_kwargs()
            # Original outputs.
            ref_outputs = export_output.output_formatter.to_onnx(model(*args, **kwargs))
            # ORT outputs.
            args_not_none = export_output.input_formatter.to_onnx(*args)
            ort_outputs = _run_ort(
                os.path.join(tmp_folder, onnx_model_location),
                args_not_none,
            )

            assert len(ref_outputs) == len(ort_outputs)

            for ref_output, ort_output in zip(ref_outputs, ort_outputs):
                torch.testing.assert_close(ref_output, torch.tensor(ort_output))

    def test_large_scale_exporter_with_toy_mlp(self):
        class MLPModel(nn.Module):
            def __init__(self):
                super().__init__()
                self.fc0 = nn.Linear(8, 8, bias=True)
                self.fc1 = nn.Linear(8, 4, bias=True)
                self.fc2 = nn.Linear(4, 2, bias=True)
                self.fc3 = nn.Linear(2, 2, bias=True)

            def forward(self, tensor_x: torch.Tensor):
                tensor_x = self.fc0(tensor_x)
                tensor_x = torch.sigmoid(tensor_x)
                tensor_x = self.fc1(tensor_x)
                tensor_x = torch.sigmoid(tensor_x)
                tensor_x = self.fc2(tensor_x)
                tensor_x = torch.sigmoid(tensor_x)
                output = self.fc3(tensor_x)
                return output

        def create_model() -> nn.Module:
            return MLPModel()

        def create_args():
            return (torch.rand((97, 8), dtype=torch.float32),)

        def create_pytorch_only_extra_kwargs():
            return {}

        self._test_large_scale_exporter(
            "toy_mlp1",
            create_model,
            create_args,
            create_pytorch_only_extra_kwargs,
            enable_dynamic_axes=False,
        )

    def test_large_scale_exporter_with_tiny_gpt2(self):
        model_name = "sshleifer/tiny-gpt2"

        def create_model() -> nn.Module:
            return transformers.AutoModel.from_pretrained(model_name)

        def create_args():
            tokenizer = transformers.AutoTokenizer.from_pretrained(model_name)
            kwargs = tokenizer("Hello world!", return_tensors="pt")
            input_ids = kwargs["input_ids"]
            attention_mask = kwargs["attention_mask"]
            return input_ids, None, attention_mask

        def create_pytorch_only_extra_kwargs():
            return {"return_dict": False}

        # FIXME(titaiwang): SegFault when symbolic tracing is used
        # https://github.com/microsoft/onnx-script/issues/523
        self._test_large_scale_exporter(
            "tiny_gpt2",
            create_model,
            create_args,
            create_pytorch_only_extra_kwargs,
            enable_dynamic_axes=False,
        )


if __name__ == "__main__":
    common_utils.run_tests()
