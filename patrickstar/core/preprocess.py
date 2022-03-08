# BSD 3-Clause License
#
# Copyright (C) 2021 THL A29 Limited, a Tencent company.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#  * Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
#  * Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
#  * Neither the name of the psutil authors nor the names of its contributors
#    may be used to endorse or promote products derived from this software without
#    specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
import contextlib
import functools

import torch

from patrickstar.core import PatrickStarClient
from patrickstar.core import register_param, is_registered, ParamType
from patrickstar.manager import _runtime_config
from patrickstar.utils import log_dist, get_rank, get_world_size
from patrickstar.utils import see_memory_usage


@contextlib.contextmanager
def torch_scope(do_allreduce=True):
    r"""All parameters initialized in this scope will not be managed in chunks."""
    _runtime_config.push()
    _runtime_config.config["use_chunk"] = False
    _runtime_config.config["do_allreduce"] = do_allreduce
    yield
    _runtime_config.pop()


# Inserts _post_init_method at the end of init method
# for all sub classes of torch.nn.Module
class InsertPostInitMethodToModuleSubClasses:
    def __enter__(self):
        def preprocess_after(f):
            @functools.wraps(f)
            def wrapper(module, *args, **kwargs):
                f(module, *args, **kwargs)
                self._post_init_method(module)

            return wrapper

        def _enable_class(cls):
            cls._old_init = cls.__init__
            cls.__init__ = preprocess_after(cls.__init__)

        def _init_subclass(cls, **kwargs):
            cls.__init__ = preprocess_after(cls.__init__)

        # Replace .__init__() for all existing subclasses of torch.nn.Module
        for subclass in torch.nn.modules.module.Module.__subclasses__():
            _enable_class(subclass)

        # holding on to the current __init__subclass__ for exit
        torch.nn.modules.module.Module._old_init_subclass = (
            torch.nn.modules.module.Module.__init_subclass__
        )

        # Replace .__init__() for future subclasses of torch.nn.Module
        torch.nn.modules.module.Module.__init_subclass__ = classmethod(_init_subclass)

        self._pre_context_exec()

    def __exit__(self, exc_type, exc_value, traceback):
        def _disable_class(cls):
            cls.__init__ = cls._old_init

        # Replace .__init__() for all existing subclasses of torch.nn.Module
        for subclass in torch.nn.modules.module.Module.__subclasses__():
            _disable_class(subclass)

        # Replace .__init__() for future subclasses of torch.nn.Module
        torch.nn.modules.module.Module.__init_subclass__ = (
            torch.nn.modules.module.Module._old_init_subclass
        )

        self._post_context_exec()
        # Now that we cleaned up the metaclass injection, raise the exception.
        if exc_type is not None:
            return False

    # To be implemented by inheriting classes
    def _post_init_method(self, module):
        pass

    def _pre_context_exec(self):
        pass

    def _post_context_exec(self):
        pass


class PSPreProcessCtx(InsertPostInitMethodToModuleSubClasses):
    """
    A context to initialize model
    """

    def __init__(
        self,
        client: PatrickStarClient,
        release_after_init=False,
        not_init=False,
    ):
        self.rank = get_rank()
        self.world_size = get_world_size()
        self.client = client
        self.param_idx = 0

        self.release_after_init = release_after_init

        self.submodule_id = -1
        self.not_init = not_init

    def _post_init_method(self, module):
        r"""The function to call at the end of the constructor of each nn.Module.

        The main functionality is registering the params to chunks and
        remove the remote tensor if `release_after_init` is False.
        """
        self.submodule_id += 1
        see_memory_usage(
            f"Before converting parmas in {module.__class__.__name__}", force=False
        )

        if not _runtime_config.use_chunk:
            for name, param in module.named_parameters(recurse=False):
                name = f"{module.__class__.__name__}.{name}_{self.param_idx}"
                self.param_idx += 1
                register_param(param, ParamType.TORCH_BASED, name)
            return

        params = []
        for name, param in module.named_parameters(recurse=False):
            name = f"{module.__class__.__name__}.{name}_{self.param_idx}"
            self.param_idx += 1
            if param.dtype == torch.float:
                register_param(param, ParamType.CHUNK_BASED, name)
                params.append(param)
            else:
                register_param(param, ParamType.TORCH_BASED, name)
                param.ps_attr._is_local = True

        self.client.append_params(params)

        for param in params:
            # Delete the memory of non local tensors
            if self.client.is_local_param(param):
                param.ps_attr._is_local = True
            else:
                param.ps_attr._is_local = False
                # TODO(jiaruifang) fix distributed init bug.
                # Check results will fail when not release_after_init.
                # As release tensor here will make the random seed generator
                # behave differently (less random number generated).
                if not self.release_after_init:
                    # Here we use a non-empty tensor for huggingface. Because it
                    # needs to initialize the weight for padding_idx.
                    param.data = torch.tensor(
                        [0], dtype=torch.float, device=param.device
                    )

    def _post_context_exec(self):
        """The callback function when the context exits.

        1. Copy param.data to fp16 and fp32 chunk based params.
        2. Append dummy chunk so that the number of chunks is an integer multiple of
            number of processes.
        """
        log_dist("Post Model Init Context")

        for chunk in self.client.chunk_list.chunks:
            if chunk.is_local():
                for param in chunk.params:
                    if not self.not_init:
                        if is_registered(param):
                            init_data = param.data
                            self.client.access(param, torch.device("cpu:0"))
                            param.data.copy_(init_data)
                            self.client.release(param)
            else:
                for param in chunk.params:
                    assert not self.client.is_local_param(param)
                    # When release_after_init is True, we will release the remote
                    # param tensor here.
                    # When release_after_init is False, this will help cast dtype of
                    # remote params to torch.half (See the NOTE below).
                    param.data = torch.tensor([], dtype=torch.half, device=param.device)

        num_chunk = len(self.client.chunk_list)
        world_size = get_world_size()
        while num_chunk % world_size != 0:
            self.client.new_dummy_chunk()
            num_chunk += 1
