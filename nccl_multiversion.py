#!/usr/bin/env python
"""Usage

pip install -r https://raw.githubusercontent.com/yaroslavvb/ncluster/master/requirements.txt
pip install -U ncluster
export AWS_ACCESS_KEY_ID=AKIAIBATdf343
export AWS_SECRET_ACCESS_KEY=z7yKEP/RhO3Olk343aiP
export AWS_DEFAULT_REGION=us-east-1

export NCLUSTER_ZONE=us-east-1b
export NCLUSTER_AWS_EFA=1
export NCLUSTER_AWS_NOEFS=1
python nccl_multiversion.py
"""


import argparse
import os

import ncluster

parser = argparse.ArgumentParser()
parser.add_argument('--name', type=str, default='sanity00')
parser.add_argument('--instance_type', type=str, default="p3dn.24xlarge")
parser.add_argument('--num_tasks', type=int, default=2, help="number of nodes")
parser.add_argument('--spot', action='store_true', help='use spot instances')
parser.add_argument('--skip_setup', action='store_true',
                    help='can use this option on reruns for slightly faster turn-around')
parser.add_argument('--image_name', type=str, default='dlami23-efa')

parser.add_argument('--force_rebuild', type=int, default=0)
parser.add_argument('--do_efa', type=int, default=1)
parser.add_argument('--do_efa_hack', type=int, default=0)
parser.add_argument('--role', type=str, default='launcher')
parser.add_argument('--nproc_per_node', type=int, default=0,
                    help='number of processes to launch, if not specified, set automatically from number of gpus on instance')
parser.add_argument('--num_gpus', type=int, default=0,
                    help='number of processes to launch, if not specified, set automatically from number of gpus on instance')

args = parser.parse_args()


def launcher():
    os.environ['NCLUSTER_AWS_FAST_ROOTDISK'] = '1'  # request disk with lots of IOPS on AWS
    job = ncluster.make_job(**vars(args))

    if not args.nproc_per_node:
        args.nproc_per_node = job.tasks[0].num_gpus
        #    MPI_HOME='/usr/local/mpi'  # for DLAMI 22

    #    job.run('export NCCL_MIN_NRINGS=16')
    job.rsync('.')

    # check that ib_uverbs are loaded, and load them if not
    # Also make sure that EFA provider is available
    for task in job.tasks:
        task.run('/usr/sbin/lsmod')
        if 'verbs' not in task.output:
            task.run('sudo /usr/sbin/modprobe ib_uverbs')
        task.run('/usr/sbin/lsmod')
        assert 'verbs' in task.output

        task.run('/opt/amazon/efa/bin/fi_info -p efa')
        assert 'provider: efa' in task.output

                
    setup_completed_fn = 'setup_completed'
    if not job.tasks[0].exists(setup_completed_fn) or args.force_rebuild:
        # install rdma core and libibverbs
        job.run('wget http://mirror.centos.org/centos/6/os/x86_64/Packages/rdma-6.9_4.1-3.el6.noarch.rpm')
        job.run('sudo yum install -y rdma-6.9_4.1-3.el6.noarch.rpm')

        job.run('wget http://mirror.centos.org/centos/6/os/x86_64/Packages/libibverbs-1.1.8-4.el6.x86_64.rpm')
        job.run('sudo yum install -y ./libibverbs-1.1.8-4.el6.x86_64.rpm')

        def nccl_build(nccl_version_tag, gitcmd):
            job.run(f'export NCCL_VERSION_TAG="{nccl_version_tag}"')
            job.run(f'export GIT_CHECKOUT_CMD="{gitcmd}"')
            job.run(f'source ~/parameterized_nccl_build.sh')

        # nccl_build('2.3.7', "git checkout v2.3.7-1")
        # nccl_build('2.4.7ms0', "git checkout dev/kwen/multi-socket")
        # nccl_build('2.4.7', "git checkout v2.4.7-1")
        nccl_build('2.4.6', "git checkout v2.4.6-1")

        # setup password-less SSH between all pairs of instances
        public_keys = {}
        for task in job.tasks:
            key_fn = '~/.ssh/id_rsa'  # this fn is special, used by default by ssh
            task.run(f"yes | ssh-keygen -t rsa -f {key_fn} -N ''")

            public_keys[task] = task.read(key_fn + '.pub')

        for task1 in job.tasks:
            task1.run('echo "StrictHostKeyChecking no" >> /etc/ssh/ssh_config',
                      sudo=True, non_blocking=True)
            for task2 in job.tasks:
                # task1 ->ssh-> task2
                task2.run(f'echo "{public_keys[task1]}" >> ~/.ssh/authorized_keys',
                          non_blocking=True)
        job.tasks[0].write(setup_completed_fn, '0')
    else:
        print(f"{setup_completed_fn} found, skipping setup")

    # launch MPI
    hosts = [task.ip for task in job.tasks]
    host_str = ','.join(hosts)

    task0 = job.tasks[0]

    #    nccl_version_tag = '2.4.7'
    #    nccl_version_tag = '2.3.7'
    #    nccl_version_tag = '2.4.7ms0'
    NCCL_VERSION_TAG = '2.4.6'
    FOLDER_ROOT = f"{task0.homedir}/nccl/nccl-{NCCL_VERSION_TAG}"
    CUDA_HOME = f'/usr/local/cuda-10.0'
    MPI_HOME = f'{task0.homedir}/anaconda3'
    NCCL_HOME = f'{FOLDER_ROOT}/nccl/build'
    EFA_HOME = f'/opt/amazon/efa'

    #    task0.run('export LD_DEBUG=libs')  # show libraries that are getting loaded


    # sanity check, simple mpirun that will print hostnames
    task0.run(f'{MPI_HOME}/bin/mpirun --host {host_str} hostname')
    
    NUM_GPUS = 8 * args.num_tasks
    NUM_GPUS = 2

    # Run through EFA on 2 gpus/2 machines
    cmd_efa = (f'{MPI_HOME}/bin/mpirun '
               f'-x FI_PROVIDER="efa" '  # Enables running nccl-tests using EFA provider.
               f'-x FI_OFI_RXR_RX_COPY_UNEXP=1 '  #  Disables using bounce buffers for unexpected messages.
               f'-x FI_OFI_RXR_RX_COPY_OOO=1 '  # Disables using bounce buffers for out of order messages.
               f'-x FI_EFA_MR_CACHE_ENABLE=1 '  # Enables memory region caching.
               f'-x FI_OFI_RXR_INLINE_MR_ENABLE=1 '  # Enables inline memory registration of data buffers.
               f'-x LD_LIBRARY_PATH='
               f'{FOLDER_ROOT}/aws-ofi-nccl/install/lib/:'
               f'{NCCL_HOME}/lib:'
               f'{CUDA_HOME}/lib64:'
               f'{EFA_HOME}/lib64:'
               f'{MPI_HOME}/lib:$LD_LIBRARY_PATH '
               f'-x NCCL_DEBUG=INFO '        # print NCCL version info
               f'-x NCCL_TREE_THRESHOLD=0 '  # Disable tree-algorithm, faster for <8 instances
               f'--host {host_str} -n {NUM_GPUS} -N 1 '
               f'--mca btl tcp,self --mca btl_tcp_if_exclude lo,docker0 '
               f'--bind-to none '
               f'--oversubscribe '           # https://github.com/NVIDIA/nccl-tests/issues/21
               f'{FOLDER_ROOT}/nccl-tests/build/all_reduce_perf -b 8 -e 1M -f 2 -g 1 -c 1 -n {NUM_GPUS}')

    if args.do_efa:
        cmd = cmd_efa
    else:
        assert False
        cmd = cmd_eth

    

    task0.run(cmd)

    print(task0.output)


def main():
    if args.role == "launcher":
        launcher()
    elif args.role == "worker":
        assert False, 'unknown arg'
    else:
        assert False, "Unknown role " + args.role


if __name__ == '__main__':
    main()
