# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License

from .common import DistributedOperatorImplContainer
from .common import DistributedOperatorImpl
from .common import register_distributed_operator_impl_container
from .common import register_distributed_operator_impl
from .common import copy_distributed_attr_for_var
from .common import copy_distributed_attr_for_dist_op
from ..utils import is_dim_shard
from ..utils import is_dim_replicate
from ..utils import is_valid_list_index
from ..utils import compute_compatible_dim_mapping
from ..utils import compute_compatible_dims_mapping
from ..utils import compute_compatible_and_update_dim_mapping
from ..dist_attribute import OperatorDistributedAttribute
from paddle.fluid import core, unique_name
from paddle.fluid.framework import in_dygraph_mode
from paddle.fluid.framework import Program, Parameter, Variable, program_guard
from paddle.fluid.data_feeder import check_variable_and_dtype, check_dtype
from paddle.distributed.fleet.meta_optimizers.common import OpRole, OP_ROLE_KEY, OP_ROLE_VAR_KEY
from ..process_group import new_process_group
from ..utils import _get_comm_group, _get_corresponding_rank


def _update_dims_mapping_for_matmul(dist_op):
    changed = False
    op_desc = dist_op.serial_op.desc
    op_dist_attr = dist_op.dist_attr
    x_name = op_desc.input('X')[0]
    y_name = op_desc.input('Y')[0]
    out_name = op_desc.output('Out')[0]
    x_dims_mapping = op_dist_attr.get_input_dims_mapping(x_name)
    y_dims_mapping = op_dist_attr.get_input_dims_mapping(y_name)
    out_dims_mapping = op_dist_attr.get_output_dims_mapping(out_name)
    x_dims_mapping_len = len(x_dims_mapping)
    y_dims_mapping_len = len(y_dims_mapping)
    out_dims_mapping_len = len(out_dims_mapping)

    # Add dim mapping to Make sure the length dims_mapping be at least 2
    if x_dims_mapping_len == 1:
        x_dims_mapping.insert(0, -1)
    if y_dims_mapping_len == 1:
        y_dims_mapping.insert(1, -1)

    # Deal with dim > 2 and take care of broadcasting
    if out_dims_mapping_len > 2:
        broadcast_x_dims_mapping = []
        broadcast_y_dims_mapping = []
        broadcast_out_dims_mapping = []

        for i in range(out_dims_mapping_len - x_dims_mapping_len):
            broadcast_x_dims_mapping.append(out_dims_mapping[i])
        for i in range(x_dims_mapping_len - 2):
            broadcast_x_dims_mapping.append(x_dims_mapping[i])

        for i in range(out_dims_mapping_len - y_dims_mapping_len):
            broadcast_y_dims_mapping.append(out_dims_mapping[i])
        for i in range(y_dims_mapping_len - 2):
            broadcast_y_dims_mapping.append(y_dims_mapping[i])

        for i in range(out_dims_mapping_len - 2):
            broadcast_out_dims_mapping.append(out_dims_mapping[i])

        compatible_dims_mapping = compute_compatible_dims_mapping([
            broadcast_x_dims_mapping, broadcast_y_dims_mapping,
            broadcast_out_dims_mapping
        ])
        assert compatible_dims_mapping is not None, "There is no compatible dim mapping."

        for i in range(x_dims_mapping_len - 2):
            new_idx = i + (out_dims_mapping_len - x_dims_mapping_len)
            if x_dims_mapping[i] != compatible_dims_mapping[new_idx]:
                x_dims_mapping[i] = compatible_dims_mapping[new_idx]
                changed = True

        for i in range(y_dims_mapping_len - 2):
            new_idx = i + (out_dims_mapping_len - y_dims_mapping_len)
            if y_dims_mapping[i] != compatible_dims_mapping[new_idx]:
                y_dims_mapping[i] = compatible_dims_mapping[new_idx]
                changed = True

        for i in range(out_dims_mapping_len - 2):
            if out_dims_mapping[i] != compatible_dims_mapping[i]:
                out_dims_mapping[i] = compatible_dims_mapping[i]
                changed = True

    # The following which uses negative index can be work
    # when len(out_dims_mapping) > 2 and len(out_dims_mapping) <=2
    dim_changed = compute_compatible_and_update_dim_mapping(
        [x_dims_mapping, y_dims_mapping], [-1, -2])
    if dim_changed:
        changed = True

    dim_changed = compute_compatible_and_update_dim_mapping(
        [x_dims_mapping, out_dims_mapping], [-2, -2])
    if dim_changed:
        changed = True

    dim_changed = compute_compatible_and_update_dim_mapping(
        [y_dims_mapping, out_dims_mapping], [-1, -1])
    if dim_changed:
        changed = True

    # Remove unnecessary dim mapping to make sure the length of dims_mapping is same as its tensor
    if x_dims_mapping_len == 1:
        x_dims_mapping.pop(0)
    if y_dims_mapping_len == 1:
        y_dims_mapping.pop(1)

    assert len(x_dims_mapping) == x_dims_mapping_len
    assert len(y_dims_mapping) == y_dims_mapping_len
    assert len(out_dims_mapping) == out_dims_mapping_len

    return changed


def _right_operand_parameter_matmul_backward(ctx, *args, **kwargs):

    # by now the backward function only insert the gradient allreduce for dist op itself

    dist_op_context = ctx.dist_op_context
    main_block = dist_op_context.get_dst_main_program().global_block()
    backward_op = dist_op_context.get_cur_src_op()
    rank_id = dist_op_context.get_rank_id()
    dist_attr = ctx.get_op_dist_attr_for_program(backward_op)
    assert dist_attr is not None, "backward op [{}] don't have dist attribute !".format(
        str(backward_op))

    # FIXME (JZ-LIANG) Remove this hack to support any op mesh group for Pipeline Parallelism
    if rank_id not in dist_attr.process_mesh.processes:
        rank_id = _get_corresponding_rank(ctx, dist_attr.process_mesh, rank_id)

    # check if need gradient allreduce
    need_gradient_allreduce = False

    assert 'Y' in kwargs, "input [{}] is not given".format('Y')
    assert 'X' in kwargs, "input [{}] is not given".format('X')
    assert 'Out@GRAD' in kwargs, "input [{}] is not given".format('Out@GRAD')
    assert 'Y@GRAD' in kwargs, "output [{}] is not given".format('Y@GRAD')
    assert 'X@GRAD' in kwargs, "output [{}] is not given".format('X@GRAD')

    assert len(
        kwargs['Y']
    ) == 1, "row_parallel_embedding input Ids take 1 variable but got {}".format(
        kwargs['Y'])
    assert len(
        kwargs['X']
    ) == 1, "row_parallel_embedding input Ids take 1 variable but got {}".format(
        kwargs['X'])
    assert len(
        kwargs['Out@GRAD']
    ) == 1, "row_parallel_embedding input Ids take 1 variable but got {}".format(
        kwargs['Out'])
    assert len(
        kwargs['Y@GRAD']
    ) == 1, "row_parallel_embedding output Ids take 1 variable but got {}".format(
        kwargs['Y@GRAD'])
    assert len(
        kwargs['X@GRAD']
    ) == 1, "row_parallel_embedding output Ids take 1 variable but got {}".format(
        kwargs['X@GRAD'])

    X_var = main_block.var(kwargs['X'][0])
    assert not X_var.is_parameter, "left operand(X) [{}] of dist matmul should not be parameter".format(
        X_var.name)

    process_mesh = dist_attr.process_mesh
    var_dim_mapping = dist_attr.get_input_dims_mapping(X_var.name)
    mesh_shape = process_mesh.topology
    batch_size_axis = var_dim_mapping[0]
    if batch_size_axis > -1 and mesh_shape[batch_size_axis] > 1:
        need_gradient_allreduce = True
        group_ranks = _get_comm_group(process_mesh.processes,
                                      process_mesh.topology, batch_size_axis,
                                      rank_id)
        dp_degree = len(group_ranks)
        dp_group = new_process_group(group_ranks)

    Y_var = main_block.var(kwargs['Y'][0])
    if need_gradient_allreduce and Y_var.is_parameter:
        Y_Grad_var = main_block.var(kwargs['Y@GRAD'][0])
        allreduce_op = main_block.append_op(
            type='c_allreduce_sum',
            inputs={'X': [Y_Grad_var]},
            outputs={'Out': [Y_Grad_var]},
            attrs={
                'ring_id': dp_group.id,
                'use_calc_stream': True,
                OP_ROLE_KEY: OpRole.Backward
            })
        scale_op = main_block.append_op(
            type='scale',
            inputs={'X': Y_Grad_var},
            outputs={'Out': Y_Grad_var},
            attrs={'scale': 1.0 / dp_degree,
                   OP_ROLE_KEY: OpRole.Backward})
        main_block._sync_with_cpp()

        dims_mapping = ctx.get_tensor_dist_attr_for_program(
            Y_Grad_var).dims_mapping
        process_mesh = dist_attr.process_mesh
        for op in [allreduce_op, scale_op]:
            op_attr = OperatorDistributedAttribute()
            op_attr.process_mesh = process_mesh
            op_attr.set_output_dims_mapping(Y_Grad_var.name, dims_mapping)
            op_attr.set_input_dims_mapping(Y_Grad_var.name, dims_mapping)
            ctx.set_op_dist_attr_for_program(op, op_attr)


def _init_param_sync(Weight_var, dist_op_context, startup_block, ctx, rank_id):

    assert Weight_var.name not in dist_op_context.already_init_sync_vars
    assert startup_block.has_var(Weight_var.name)
    dist_op_context.already_init_sync_vars.add(Weight_var.name)
    param = startup_block.var(Weight_var.name)
    param_dist_attr = ctx.get_tensor_dist_attr_for_program(param)
    process_mesh = param_dist_attr.process_mesh
    dim_mapping = param_dist_attr.dims_mapping

    for axis, size in enumerate(process_mesh.topology):
        if size <= 1 or axis in dim_mapping:
            pass
        else:
            group_ranks = _get_comm_group(process_mesh.processes,
                                          process_mesh.topology, axis, rank_id)
            sync_group = new_process_group(group_ranks)

            startup_block.append_op(
                type='c_broadcast',
                inputs={'X': param},
                outputs={'Out': param},
                attrs={
                    'ring_id': sync_group.id,
                    'root': 0,
                    'use_calc_stream': True,
                    OP_ROLE_KEY: OpRole.Forward
                })
    startup_block._sync_with_cpp()


class DistributedMatmul(DistributedOperatorImplContainer):
    def __init__(self, name):
        super(DistributedMatmul, self).__init__()
        self._name = name


register_distributed_operator_impl_container("matmul",
                                             DistributedMatmul("matmul"))


# ColumnParallel
class DistributedMatmulImpl0(DistributedOperatorImpl):
    def __init__(self, name):
        super(DistributedMatmulImpl0, self).__init__()
        self._name = name
        self._forward_implemented = True
        self._backward_implemented = True

    def is_input_compatible(self, dist_op):
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        x_name = op_desc.input('X')[0]
        y_name = op_desc.input('Y')[0]
        x_dims_mapping = op_dist_attr.get_input_dims_mapping(x_name)
        y_dims_mapping = op_dist_attr.get_input_dims_mapping(y_name)
        if is_dim_shard(x_dims_mapping[-1]):
            return False
        if is_dim_shard(y_dims_mapping[0]) or is_dim_replicate(y_dims_mapping[
                1]):
            return False
        for mapping in x_dims_mapping[1:-1]:
            if is_dim_shard(mapping):
                return False
        return True

    def is_output_compatible(self, dist_op):
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        out_name = op_desc.output('Out')[0]
        out_dims_mapping = op_dist_attr.get_output_dims_mapping(out_name)
        if is_dim_replicate(out_dims_mapping[-1]):
            return False
        for mapping in out_dims_mapping[1:-1]:
            if is_dim_shard(mapping):
                return False
        return True

    def update_dims_mapping(self, dist_op):
        changed = False
        dim_changed = _update_dims_mapping_for_matmul(dist_op)
        if dim_changed:
            changed = True
        return changed

    @staticmethod
    def forward(ctx, *args, **kwargs):
        """
        kwargs: inputname_mapping & outputname_mapping
        """

        dist_op_context = ctx.dist_op_context
        main_block = dist_op_context.get_dst_main_program().global_block()
        startup_block = dist_op_context.get_dst_startup_program().global_block()
        src_op = dist_op_context.get_cur_src_op()
        rank_id = dist_op_context.get_rank_id()
        op_dist_attr = ctx.get_op_dist_attr_for_program(src_op)
        assert op_dist_attr is not None, "backward op [{}] don't have dist attribute !".format(
            str(src_op))

        # FIXME (JZ-LIANG) Remove this hack to support any op mesh group for Pipeline Parallelism
        if rank_id not in op_dist_attr.process_mesh.processes:
            rank_id = _get_corresponding_rank(ctx, op_dist_attr.process_mesh,
                                              rank_id)

        # check validation of inputs / outputs
        for input_name in src_op.desc.input_names():
            assert input_name in kwargs, "input [{}] is not given".format(
                input_name)
            assert len(kwargs[input_name]) == len(
                src_op.desc.input(input_name)
            ), "number of tensor for input [{}] is not match".format(input_name)
        for output_name in src_op.desc.output_names():
            assert output_name in kwargs, "input [{}] is not given".format(
                output_name)
            assert len(kwargs[output_name]) == len(
                src_op.desc.output(output_name)
            ), "number of tensor for input [{}] is not match".format(
                output_name)

        X_var = main_block.var(kwargs['X'][0])
        Weight_var = main_block.var(kwargs['Y'][0])
        Out_var = main_block.var(kwargs['Out'][0])

        # TODO infer logic comm presentation
        matmul_col_dim_mapping = op_dist_attr.get_input_dims_mapping(
            Weight_var.name)[1]
        assert matmul_col_dim_mapping >= 0, "col_parallel_matmul's row should be divided by a specific mesh axis, but got [{}]".format(
            matmul_col_dim_mapping)
        process_mesh_shape = op_dist_attr.process_mesh.topology
        process_mesh_group = op_dist_attr.process_mesh.processes

        parallel_axis = matmul_col_dim_mapping
        group_ranks = _get_comm_group(process_mesh_group, process_mesh_shape,
                                      parallel_axis, rank_id)
        group = new_process_group(group_ranks)

        intermediate_var_0 = main_block.create_var(
            name=unique_name.generate_with_ignorable_key(".".join(
                ["c_identity", 'tmp'])),
            dtype=X_var.dtype,
            shape=X_var.shape,
            type=core.VarDesc.VarType.LOD_TENSOR,
            persistable=False,
            stop_gradient=X_var.stop_gradient)
        # copy X_var's dist_attr to intermediate_var_0's dist_attr
        copy_distributed_attr_for_var(ctx, intermediate_var_0, X_var)

        check_variable_and_dtype(
            X_var, 'tensor',
            ['float16', 'float32', 'float64', 'int32', 'int64'], '_c_identity')

        c_identity_op = main_block.append_op(
            type='c_identity',
            inputs={'X': [X_var]},
            outputs={'Out': intermediate_var_0},
            attrs={
                'ring_id': group.id,
                'use_calc_stream': True,
                'use_model_parallel': True,
            })

        check_variable_and_dtype(intermediate_var_0, 'x',
                                 ['float16', 'float32', 'float64'], 'linear')
        check_dtype(intermediate_var_0.dtype, 'dtype',
                    ['float16', 'float32', 'float64'], 'linear')
        attrs = {
            'transpose_X': False,
            'transpose_Y': False,
            'alpha': 1,
        }
        inputs = {'X': [intermediate_var_0], 'Y': [Weight_var]}
        matmul_op = main_block.append_op(
            type='matmul', inputs=inputs, outputs={'Out': Out_var}, attrs=attrs)

        # copy serial op's dist_attr to dist op's dist_attr
        copy_distributed_attr_for_dist_op(ctx, c_identity_op, main_block,
                                          op_dist_attr)
        copy_distributed_attr_for_dist_op(ctx, matmul_op, main_block,
                                          op_dist_attr)

        # init param sync
        if Weight_var.is_parameter:
            _init_param_sync(Weight_var, dist_op_context, startup_block, ctx,
                             rank_id)

    @staticmethod
    def backward(ctx, *args, **kwargs):
        _right_operand_parameter_matmul_backward(ctx, *args, **kwargs)


# RowParallel
class DistributedMatmulImpl1(DistributedOperatorImpl):
    def __init__(self, name):
        super(DistributedMatmulImpl1, self).__init__()
        self._name = name
        self._forward_implemented = True
        self._backward_implemented = True

    def is_input_compatible(self, dist_op):
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        x_name = op_desc.input('X')[0]
        y_name = op_desc.input('Y')[0]
        x_dims_mapping = op_dist_attr.get_input_dims_mapping(x_name)
        y_dims_mapping = op_dist_attr.get_input_dims_mapping(y_name)
        if is_dim_replicate(x_dims_mapping[-1]):
            return False
        if is_dim_replicate(y_dims_mapping[-2]) or is_dim_shard(y_dims_mapping[
                -1]):
            return False
        # Other dimensions must be replicate except the batch dimension
        for mapping in x_dims_mapping[1:-1]:
            if is_dim_shard(mapping):
                return False
        return True

    def is_output_compatible(self, dist_op):
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        out_name = op_desc.output('Out')[0]
        out_dims_mapping = op_dist_attr.get_output_dims_mapping(out_name)
        if is_dim_shard(out_dims_mapping[-1]):
            return False
        # Other dimensions must be replicate except the batch dimension
        for mapping in out_dims_mapping[1:-1]:
            if is_dim_shard(mapping):
                return False
        return True

    def update_dims_mapping(self, dist_op):
        changed = False
        dim_changed = _update_dims_mapping_for_matmul(dist_op)
        if dim_changed:
            changed = True
        return changed

    @staticmethod
    def forward(ctx, *args, **kwargs):
        """
        kwargs: inputname_mapping & outputname_mapping
        """

        dist_op_context = ctx.dist_op_context
        main_block = dist_op_context.get_dst_main_program().global_block()
        startup_block = dist_op_context.get_dst_startup_program().global_block()
        src_op = dist_op_context.get_cur_src_op()
        rank_id = dist_op_context.get_rank_id()
        op_dist_attr = ctx.get_op_dist_attr_for_program(src_op)
        assert op_dist_attr is not None, "backward op [{}] don't have dist attribute !".format(
            str(src_op))

        # FIXME (JZ-LIANG) Remove this hack to support any op mesh group for Pipeline Parallelism
        if rank_id not in op_dist_attr.process_mesh.processes:
            rank_id = _get_corresponding_rank(ctx, op_dist_attr.process_mesh,
                                              rank_id)

        # check validation of inputs / outputs
        for input_name in src_op.desc.input_names():
            assert input_name in kwargs, "input [{}] is not given".format(
                input_name)
            assert len(kwargs[input_name]) == len(
                src_op.desc.input(input_name)
            ), "number of tensor for input [{}] is not match".format(input_name)
        for output_name in src_op.desc.output_names():
            assert output_name in kwargs, "input [{}] is not given".format(
                output_name)
            assert len(kwargs[output_name]) == len(
                src_op.desc.output(output_name)
            ), "number of tensor for input [{}] is not match".format(
                output_name)

        X_var = main_block.var(kwargs['X'][0])
        Weight_var = main_block.var(kwargs['Y'][0])
        Out_var = main_block.var(kwargs['Out'][0])

        # TODO infer logic comm presentation
        matmul_row_dim_mapping = op_dist_attr.get_input_dims_mapping(
            Weight_var.name)[0]
        assert matmul_row_dim_mapping >= 0, "row_parallel_matmul's row should be divided by a specific mesh axis, but got [{}]".format(
            matmul_row_dim_mapping)
        process_mesh_shape = op_dist_attr.process_mesh.topology
        process_mesh_group = op_dist_attr.process_mesh.processes

        parallel_axis = matmul_row_dim_mapping
        group_ranks = _get_comm_group(process_mesh_group, process_mesh_shape,
                                      parallel_axis, rank_id)
        group = new_process_group(group_ranks)

        check_variable_and_dtype(X_var, 'x', ['float16', 'float32', 'float64'],
                                 'linear')
        check_dtype(X_var.dtype, 'dtype', ['float16', 'float32', 'float64'],
                    'linear')
        attrs = {
            'transpose_X': False,
            'transpose_Y': False,
            'alpha': 1,
        }
        inputs = {'X': X_var, 'Y': Weight_var}
        intermediate_var_0 = main_block.create_var(
            shape=Out_var.shape,
            dtype=Out_var.dtype,
            type=Out_var.type,
            lod_level=Out_var.lod_level,
            persistable=False,
            is_data=False,
            need_check_feed=Out_var.desc.need_check_feed())
        # copy Out_var's dist_attr to intermediate_var_0's dist_attr
        copy_distributed_attr_for_var(ctx, intermediate_var_0, Out_var)

        matmul_op = main_block.append_op(
            type='matmul',
            inputs=inputs,
            outputs={'Out': intermediate_var_0},
            attrs=attrs)

        c_allreduce_sum_op = main_block.append_op(
            type='c_allreduce_sum',
            inputs={'X': intermediate_var_0},
            outputs={'Out': Out_var},
            attrs={
                'ring_id': group.id,
                'use_calc_stream': True,
                'use_model_parallel': True
            })

        # copy serial op's dist_attr to dist op's dist_attr
        copy_distributed_attr_for_dist_op(ctx, matmul_op, main_block,
                                          op_dist_attr)
        copy_distributed_attr_for_dist_op(ctx, c_allreduce_sum_op, main_block,
                                          op_dist_attr)

        # init param sync
        if Weight_var.is_parameter:
            _init_param_sync(Weight_var, dist_op_context, startup_block, ctx,
                             rank_id)

    @staticmethod
    def backward(ctx, *args, **kwargs):
        _right_operand_parameter_matmul_backward(ctx, *args, **kwargs)


# ReplicateParallel
class DistributedMatmulImpl2(DistributedOperatorImpl):
    def __init__(self, name):
        super(DistributedMatmulImpl2, self).__init__()
        self._name = name

    def is_input_compatible(self, dist_op):
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        x_name = op_desc.input('X')[0]
        y_name = op_desc.input('Y')[0]
        x_dims_mapping = op_dist_attr.get_input_dims_mapping(x_name)
        y_dims_mapping = op_dist_attr.get_input_dims_mapping(y_name)

        if is_dim_shard(x_dims_mapping[-1]):
            return False
        if is_valid_list_index(x_dims_mapping,
                               -2) and is_dim_shard(x_dims_mapping[-2]):
            return False

        if is_dim_shard(y_dims_mapping[-1]):
            return False
        if is_valid_list_index(y_dims_mapping,
                               -2) and is_dim_shard(y_dims_mapping[-2]):
            return False

        return True

    def is_output_compatible(self, dist_op):
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        out_name = op_desc.output('Out')[0]
        out_dims_mapping = op_dist_attr.get_output_dims_mapping(out_name)

        if is_dim_shard(out_dims_mapping[-1]):
            return False
        if is_valid_list_index(out_dims_mapping,
                               -2) and is_dim_shard(out_dims_mapping[-2]):
            return False

        return True

    def update_dims_mapping(self, dist_op):
        changed = False
        dim_changed = _update_dims_mapping_for_matmul(dist_op)
        if dim_changed:
            changed = True
        return changed

    @staticmethod
    def backward(ctx, *args, **kwargs):
        _right_operand_parameter_matmul_backward(ctx, *args, **kwargs)


register_distributed_operator_impl("matmul",
                                   DistributedMatmulImpl0("column_parallel"))
register_distributed_operator_impl("matmul",
                                   DistributedMatmulImpl1("row_parallel"))
register_distributed_operator_impl("matmul",
                                   DistributedMatmulImpl2("replicate_parallel"))


class DistributedMatmulV2(DistributedOperatorImplContainer):
    def __init__(self, name):
        super(DistributedMatmulV2, self).__init__()
        self._name = name


register_distributed_operator_impl_container("matmul_v2",
                                             DistributedMatmulV2("matmul_v2"))


# ColumnParallel
class DistributedMatmulV2Impl0(DistributedOperatorImpl):
    def __init__(self, name):
        super(DistributedMatmulV2Impl0, self).__init__()
        self._name = name
        self._forward_implemented = True
        self._backward_implemented = True

    def is_input_compatible(self, dist_op):
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        x_name = op_desc.input('X')[0]
        y_name = op_desc.input('Y')[0]
        x_dims_mapping = op_dist_attr.get_input_dims_mapping(x_name)
        y_dims_mapping = op_dist_attr.get_input_dims_mapping(y_name)
        if is_dim_shard(x_dims_mapping[-1]):
            return False
        if is_dim_shard(y_dims_mapping[0]) or is_dim_replicate(y_dims_mapping[
                1]):
            return False
        for mapping in x_dims_mapping[1:-1]:
            if is_dim_shard(mapping):
                return False
        return True

    def is_output_compatible(self, dist_op):
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        out_name = op_desc.output('Out')[0]
        out_dims_mapping = op_dist_attr.get_output_dims_mapping(out_name)
        if is_dim_replicate(out_dims_mapping[-1]):
            return False
        for mapping in out_dims_mapping[1:-1]:
            if is_dim_shard(mapping):
                return False
        return True

    def update_dims_mapping(self, dist_op):
        changed = False
        dim_changed = _update_dims_mapping_for_matmul(dist_op)
        if dim_changed:
            changed = True
        return changed

    @staticmethod
    def forward(ctx, *args, **kwargs):
        """
        kwargs: inputname_mapping & outputname_mapping
        """

        dist_op_context = ctx.dist_op_context
        main_block = dist_op_context.get_dst_main_program().global_block()
        startup_block = dist_op_context.get_dst_startup_program().global_block()
        src_op = dist_op_context.get_cur_src_op()
        rank_id = dist_op_context.get_rank_id()
        op_dist_attr = ctx.get_op_dist_attr_for_program(src_op)
        assert op_dist_attr is not None, "backward op [{}] don't have dist attribute !".format(
            str(src_op))

        # FIXME (JZ-LIANG) Remove this hack to support any op mesh group for Pipeline Parallelism
        if rank_id not in op_dist_attr.process_mesh.processes:
            rank_id = _get_corresponding_rank(ctx, op_dist_attr.process_mesh,
                                              rank_id)

        # check validation of inputs / outputs
        for input_name in src_op.desc.input_names():
            assert input_name in kwargs, "input [{}] is not given".format(
                input_name)
            assert len(kwargs[input_name]) == len(
                src_op.desc.input(input_name)
            ), "number of tensor for input [{}] is not match".format(input_name)
        for output_name in src_op.desc.output_names():
            assert output_name in kwargs, "input [{}] is not given".format(
                output_name)
            assert len(kwargs[output_name]) == len(
                src_op.desc.output(output_name)
            ), "number of tensor for input [{}] is not match".format(
                output_name)

        X_var = main_block.var(kwargs['X'][0])
        Weight_var = main_block.var(kwargs['Y'][0])
        Out_var = main_block.var(kwargs['Out'][0])

        # TODO infer logic comm presentation
        matmul_col_dim_mapping = op_dist_attr.get_input_dims_mapping(
            Weight_var.name)[1]
        assert matmul_col_dim_mapping >= 0, "col_parallel_matmul's row should be divided by a specific mesh axis, but got [{}]".format(
            matmul_col_dim_mapping)
        process_mesh_shape = op_dist_attr.process_mesh.topology
        process_mesh_group = op_dist_attr.process_mesh.processes

        parallel_axis = matmul_col_dim_mapping
        group_ranks = _get_comm_group(process_mesh_group, process_mesh_shape,
                                      parallel_axis, rank_id)
        group = new_process_group(group_ranks)

        intermediate_var_0 = main_block.create_var(
            name=unique_name.generate_with_ignorable_key(".".join(
                ["c_identity", 'tmp'])),
            dtype=X_var.dtype,
            shape=X_var.shape,
            type=core.VarDesc.VarType.LOD_TENSOR,
            persistable=False,
            stop_gradient=X_var.stop_gradient)
        # copy X_var's dist_attr to intermediate_var_0's dist_attr
        copy_distributed_attr_for_var(ctx, intermediate_var_0, X_var)

        check_variable_and_dtype(
            X_var, 'tensor',
            ['float16', 'float32', 'float64', 'int32', 'int64'], '_c_identity')

        c_identity_op = main_block.append_op(
            type='c_identity',
            inputs={'X': [X_var]},
            outputs={'Out': intermediate_var_0},
            attrs={
                'ring_id': group.id,
                'use_calc_stream': True,
                'use_model_parallel': True,
            })

        check_variable_and_dtype(intermediate_var_0, 'x',
                                 ['float16', 'float32', 'float64'], 'linear')
        check_dtype(intermediate_var_0.dtype, 'dtype',
                    ['float16', 'float32', 'float64'], 'linear')
        attrs = {'trans_x': False, 'trans_y': False}
        inputs = {'X': [intermediate_var_0], 'Y': [Weight_var]}
        matmul_v2_op = main_block.append_op(
            type='matmul_v2',
            inputs=inputs,
            outputs={'Out': Out_var},
            attrs=attrs)

        # copy serial op's dist_attr to dist op's dist_attr
        copy_distributed_attr_for_dist_op(ctx, c_identity_op, main_block,
                                          op_dist_attr)
        copy_distributed_attr_for_dist_op(ctx, matmul_v2_op, main_block,
                                          op_dist_attr)

        # init param sync
        if Weight_var.is_parameter:
            _init_param_sync(Weight_var, dist_op_context, startup_block, ctx,
                             rank_id)

    @staticmethod
    def backward(ctx, *args, **kwargs):
        _right_operand_parameter_matmul_backward(ctx, *args, **kwargs)


# RowParallel
class DistributedMatmulV2Impl1(DistributedOperatorImpl):
    def __init__(self, name):
        super(DistributedMatmulV2Impl1, self).__init__()
        self._name = name
        self._forward_implemented = True
        self._backward_implemented = True

    def is_input_compatible(self, dist_op):
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        x_name = op_desc.input('X')[0]
        y_name = op_desc.input('Y')[0]
        x_dims_mapping = op_dist_attr.get_input_dims_mapping(x_name)
        y_dims_mapping = op_dist_attr.get_input_dims_mapping(y_name)
        if is_dim_replicate(x_dims_mapping[-1]):
            return False
        if is_dim_replicate(y_dims_mapping[-2]) or is_dim_shard(y_dims_mapping[
                -1]):
            return False
        # Other dimensions must be replicate except the batch dimension
        for mapping in x_dims_mapping[1:-1]:
            if is_dim_shard(mapping):
                return False
        return True

    def is_output_compatible(self, dist_op):
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        out_name = op_desc.output('Out')[0]
        out_dims_mapping = op_dist_attr.get_output_dims_mapping(out_name)
        if is_dim_shard(out_dims_mapping[-1]):
            return False
        # Other dimensions must be replicate except the batch dimension
        for mapping in out_dims_mapping[1:-1]:
            if is_dim_shard(mapping):
                return False
        return True

    def update_dims_mapping(self, dist_op):
        changed = False
        dim_changed = _update_dims_mapping_for_matmul(dist_op)
        if dim_changed:
            changed = True
        return changed

    @staticmethod
    def forward(ctx, *args, **kwargs):
        """
        kwargs: inputname_mapping & outputname_mapping
        """

        dist_op_context = ctx.dist_op_context
        main_block = dist_op_context.get_dst_main_program().global_block()
        startup_block = dist_op_context.get_dst_startup_program().global_block()
        src_op = dist_op_context.get_cur_src_op()
        rank_id = dist_op_context.get_rank_id()
        op_dist_attr = ctx.get_op_dist_attr_for_program(src_op)
        assert op_dist_attr is not None, "backward op [{}] don't have dist attribute !".format(
            str(src_op))

        # FIXME (JZ-LIANG) Remove this hack to support any op mesh group for Pipeline Parallelism
        if rank_id not in op_dist_attr.process_mesh.processes:
            rank_id = _get_corresponding_rank(ctx, op_dist_attr.process_mesh,
                                              rank_id)

        # check validation of inputs / outputs
        for input_name in src_op.desc.input_names():
            assert input_name in kwargs, "input [{}] is not given".format(
                input_name)
            assert len(kwargs[input_name]) == len(
                src_op.desc.input(input_name)
            ), "number of tensor for input [{}] is not match".format(input_name)
        for output_name in src_op.desc.output_names():
            assert output_name in kwargs, "input [{}] is not given".format(
                output_name)
            assert len(kwargs[output_name]) == len(
                src_op.desc.output(output_name)
            ), "number of tensor for input [{}] is not match".format(
                output_name)

        X_var = main_block.var(kwargs['X'][0])
        Weight_var = main_block.var(kwargs['Y'][0])
        Out_var = main_block.var(kwargs['Out'][0])

        # TODO infer logic comm presentation
        matmul_row_dim_mapping = op_dist_attr.get_input_dims_mapping(
            Weight_var.name)[0]
        assert matmul_row_dim_mapping >= 0, "row_parallel_matmul's row should be divided by a specific mesh axis, but got [{}]".format(
            matmul_row_dim_mapping)
        process_mesh_shape = op_dist_attr.process_mesh.topology
        process_mesh_group = op_dist_attr.process_mesh.processes

        parallel_axis = matmul_row_dim_mapping
        group_ranks = _get_comm_group(process_mesh_group, process_mesh_shape,
                                      parallel_axis, rank_id)
        group = new_process_group(group_ranks)

        check_variable_and_dtype(X_var, 'x', ['float16', 'float32', 'float64'],
                                 'linear')
        check_dtype(X_var.dtype, 'dtype', ['float16', 'float32', 'float64'],
                    'linear')
        attrs = {'trans_x': False, 'trans_y': False}
        inputs = {'X': X_var, 'Y': Weight_var}
        intermediate_var_0 = main_block.create_var(
            shape=Out_var.shape,
            dtype=Out_var.dtype,
            type=Out_var.type,
            lod_level=Out_var.lod_level,
            persistable=False,
            is_data=False,
            need_check_feed=Out_var.desc.need_check_feed())
        # copy Out_var's dist_attr to intermediate_var_0's dist_attr
        copy_distributed_attr_for_var(ctx, intermediate_var_0, Out_var)

        matmul_v2_op = main_block.append_op(
            type='matmul_v2',
            inputs=inputs,
            outputs={'Out': intermediate_var_0},
            attrs=attrs)

        c_allreduce_sum_op = main_block.append_op(
            type='c_allreduce_sum',
            inputs={'X': intermediate_var_0},
            outputs={'Out': Out_var},
            attrs={
                'ring_id': group.id,
                'use_calc_stream': True,
                'use_model_parallel': True
            })

        # copy serial op's dist_attr to dist op's dist_attr
        copy_distributed_attr_for_dist_op(ctx, matmul_v2_op, main_block,
                                          op_dist_attr)
        copy_distributed_attr_for_dist_op(ctx, c_allreduce_sum_op, main_block,
                                          op_dist_attr)

        # init param sync
        if Weight_var.is_parameter:
            _init_param_sync(Weight_var, dist_op_context, startup_block, ctx,
                             rank_id)

    @staticmethod
    def backward(ctx, *args, **kwargs):
        _right_operand_parameter_matmul_backward(ctx, *args, **kwargs)


# ReplicateParallel
class DistributedMatmulV2Impl2(DistributedOperatorImpl):
    def __init__(self, name):
        super(DistributedMatmulV2Impl2, self).__init__()
        self._name = name

    def is_input_compatible(self, dist_op):
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        x_name = op_desc.input('X')[0]
        y_name = op_desc.input('Y')[0]
        x_dims_mapping = op_dist_attr.get_input_dims_mapping(x_name)
        y_dims_mapping = op_dist_attr.get_input_dims_mapping(y_name)

        if is_dim_shard(x_dims_mapping[-1]):
            return False
        if is_valid_list_index(x_dims_mapping,
                               -2) and is_dim_shard(x_dims_mapping[-2]):
            return False

        if is_dim_shard(y_dims_mapping[-1]):
            return False
        if is_valid_list_index(y_dims_mapping,
                               -2) and is_dim_shard(y_dims_mapping[-2]):
            return False

        return True

    def is_output_compatible(self, dist_op):
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        op_desc = dist_op.serial_op.desc
        op_dist_attr = dist_op.dist_attr
        out_name = op_desc.output('Out')[0]
        out_dims_mapping = op_dist_attr.get_output_dims_mapping(out_name)

        if is_dim_shard(out_dims_mapping[-1]):
            return False
        if is_valid_list_index(out_dims_mapping,
                               -2) and is_dim_shard(out_dims_mapping[-2]):
            return False

        return True

    def update_dims_mapping(self, dist_op):
        changed = False
        dim_changed = _update_dims_mapping_for_matmul(dist_op)
        if dim_changed:
            changed = True
        return changed

    @staticmethod
    def backward(ctx, *args, **kwargs):
        _right_operand_parameter_matmul_backward(ctx, *args, **kwargs)


register_distributed_operator_impl("matmul_v2",
                                   DistributedMatmulV2Impl0("column_parallel"))
register_distributed_operator_impl("matmul_v2",
                                   DistributedMatmulV2Impl1("row_parallel"))
register_distributed_operator_impl(
    "matmul_v2", DistributedMatmulV2Impl2("replicate_parallel"))
