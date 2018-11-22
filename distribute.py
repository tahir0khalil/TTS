# inspired from https://github.com/fastai/imagenet-fast/blob/master/imagenet_nv/distributed.py

import os
import sys
import time
import subprocess
import argparse
import torch
import torch.distributed as dist
from torch.autograd import Variable
from torch._utils import _flatten_dense_tensors, _unflatten_dense_tensors
from utils.generic_utils import load_config, create_experiment_folder


def reduce_tensor(tensor, num_gpus):
    rt = tensor.clone()
    dist.all_reduce(rt, op=dist.reduce_op.SUM)
    rt /= num_gpus
    return rt


def init_distributed(rank, num_gpus, group_name, dist_backend, dist_url):
    assert torch.cuda.is_available(), "Distributed mode requires CUDA."
    print("Initializing Distributed")

    # Set cuda device so everything is done on the right GPU.
    torch.cuda.set_device(rank % torch.cuda.device_count())

    # Initialize distributed communication
    dist.init_process_group(
        dist_backend,
        init_method=dist_url,
        world_size=num_gpus,
        rank=rank,
        group_name=group_name)


def apply_gradient_allreduce(module):

    for p in module.state_dict().values():
        if not torch.is_tensor(p):
            continue
        dist.broadcast(p, 0)

    def allreduce_params():
        if (module.needs_reduction):
            module.needs_reduction = False
            buckets = {}
            for param in module.parameters():
                if param.requires_grad and param.grad is not None:
                    tp = type(param.data)
                    if tp not in buckets:
                        buckets[tp] = []
                    buckets[tp].append(param)
            for tp in buckets:
                bucket = buckets[tp]
                grads = [param.grad.data for param in bucket]
                coalesced = _flatten_dense_tensors(grads)
                dist.all_reduce(coalesced)
                coalesced /= dist.get_world_size()
                for buf, synced in zip(
                        grads, _unflatten_dense_tensors(coalesced, grads)):
                    buf.copy_(synced)

    for param in list(module.parameters()):

        def allreduce_hook(*unused):
            Variable._execution_engine.queue_callback(allreduce_params)

        if param.requires_grad:
            param.register_hook(allreduce_hook)
            dir(param)

    def set_needs_reduction(self, input, output):
        self.needs_reduction = True

    module.register_forward_hook(set_needs_reduction)
    return module


def main(args):
    """
    Call train.py as a new process and pass command arguments
    """
    CONFIG = load_config(args.config_path)
    OUT_PATH = create_experiment_folder(CONFIG.output_path, CONFIG.model_name,
                                        True)
    stdout_path = os.path.join(OUT_PATH, "process_stdout/")

    num_gpus = torch.cuda.device_count()
    group_id = time.strftime("%Y_%m_%d-%H%M%S")

    # set arguments for train.py
    command = ['train.py']
    command.append('--restore_path={}'.format(args.restore_path))
    command.append('--config_path={}'.format(args.config_path))
    command.append('--group_id=group_{}'.format(group_id))
    command.append('--data_path={}'.format(args.data_path))
    command.append('--output_path={}'.format(OUT_PATH))
    command.append('')

    if not os.path.isdir(stdout_path):
        os.makedirs(stdout_path)
        os.chmod(stdout_path, 0o775)

    # run processes
    processes = []
    for i in range(num_gpus):
        command[6] = '--rank={}'.format(i)
        stdout = None if i == 0 else open(
            os.path.join(stdout_path, "process_{}.log".format(i)), "w")
        p = subprocess.Popen(["python"] + command, stdout=stdout)
        processes.append(p)
        print(command)

    for p in processes:
        p.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--restore_path',
        type=str,
        help='Folder path to checkpoints',
        default='')
    parser.add_argument(
        '--config_path',
        type=str,
        help='path to config file for training',
    )
    parser.add_argument(
        '--data_path', type=str, help='dataset path.', default='')

    args = parser.parse_args()
    main(args)