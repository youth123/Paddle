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
# limitations under the License.


def select_scatter():
    import numpy as np
    import paddle
    from paddle.distributed import init_parallel_env
    # paddle.enable_static()
    init_parallel_env()
    n_expert = 2
    world_size = 2
    d_model = 2
    in_feat = d_model
    local_input_buf = np.array(
        [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]], dtype=np.float32)
    if paddle.distributed.ParallelEnv().local_rank == 0:
        local_expert_count = np.array(
            [2, 1, 1, 1])  # (world_size * num_experts)
        global_expert_count = np.array(
            [2, 1, 1, 1])  # (world_size * num_experts)
    else:
        local_expert_count = np.array(
            [1, 1, 2, 1])  # (world_size * num_experts)
        global_expert_count = np.array(
            [1, 1, 2, 1])  # (world_size * num_experts)
    input_buf = np.empty(
        shape=(np.sum(global_expert_count), in_feat), dtype=np.
        float32)  # (batch_size, d_model) batch_size是global_expert_count的所有数量
    local_input_buf = paddle.to_tensor(local_input_buf, dtype="float32")
    local_expert_count = paddle.to_tensor(local_expert_count, dtype="int32")
    global_expert_count = paddle.to_tensor(global_expert_count, dtype="int32")
    input_buf = paddle.to_tensor(input_buf, dtype="float32")
    in_feat = paddle.to_tensor(in_feat, dtype="int32")
    n_expert = paddle.to_tensor(n_expert, dtype="int32")
    world_size = paddle.to_tensor(world_size, dtype="int32")


    a = paddle.distributed.selectscatter(local_input_buf, \
    local_expert_count, global_expert_count, \
    input_buf, in_feat, n_expert, world_size, out_tensor_list=[])
    print(a)
    # out for rank 0: [[1, 2], [3, 4], [5, 6], [1, 2], [3, 4]]
    # out for rank 1: [[7, 8], [5, 6], [7, 8], [9, 10], [9, 10]]


select_scatter()
