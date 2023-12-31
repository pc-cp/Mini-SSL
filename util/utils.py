""" Utils for working with SSL models """

# Copyright (c) 2020. Lightly AG and its affiliates.
# All Rights Reserved

import math
import warnings
from typing import Iterable, List, Optional, Tuple, Union

import torch
import torch.distributed as dist
import torch.nn as nn
from torch.nn import Module
from torch.nn.modules.batchnorm import _BatchNorm
from torch.nn.parameter import Parameter
import numpy as np
import random
import pandas as pd
from sklearn.manifold import TSNE
import seaborn as sns
import matplotlib.pyplot as plt
import os

def generation_mask(topk, batch):
    """Pseudo label generation for the introduction of top-k nearest neighbor methods.
        This code was taken and adapted from here:
        ###

        Args:
            topk:
                Number of neighbors
            batch:
                batch_size

        Returns:
            mask:
                Pseudo-labeling with dimensions [batch, batch*topk]
    Examples:
    >>> generation_mask(2, 3)
    Out[5]:
    tensor([[1., 1., 0., 0., 0., 0.],
            [0., 0., 1., 1., 0., 0.],
            [0., 0., 0., 0., 1., 1.]])
    """
    mask_ = torch.eye(batch)
    mask = mask_.repeat(topk, 1).reshape(topk, batch, -1).permute(2, 1, 0).reshape(batch, topk * batch)
    return mask

def tsne_plot(save_dir, targets, outputs, epoch):
    print('generating t-SNE plot...')
    tsne = TSNE(random_state=epoch)
    tsne_output = tsne.fit_transform(outputs)

    df = pd.DataFrame(tsne_output, columns=['x', 'y'])
    df['classes'] = targets

    plt.rcParams['figure.figsize'] = 10, 10
    sns.scatterplot(
        x='x', y='y',
        hue='classes',
        palette=sns.color_palette("hls", 10),
        data=df,
        marker='o',
        legend="full",
        alpha=0.5
    )

    plt.xticks([])
    plt.yticks([])
    plt.xlabel('')
    plt.ylabel('')

    plt.savefig(os.path.join(save_dir, 'tsne'+str(epoch)+'.png'), bbox_inches='tight', dpi=100)
    plt.clf()
    print('done!')



class LinearHead(nn.Module):
    """Classifiers for downstream tasks.

        This code was taken and adapted from here:
        # https://github.com/mingkai-zheng/ReSSL/blob/4d67daaa3fd65e81adeb02017a1cfd4d2e2168cb/network/head.py#L6

        Args:
            net:
                Pre-trained backbone
            dim_in:
                Embedding dimension of sample features
            num_class:
                Number of classes in the dataset

        Returns:
            Classification of downstream tasks
    """
    def __init__(self, net, dim_in=2048, num_class=1000):
        super().__init__()
        self.net = net
        self.fc = nn.Linear(dim_in, num_class)

        for param in self.net.parameters():
            param.requires_grad = False

        self.fc.weight.data.normal_(mean=0.0, std=0.01)
        self.fc.bias.data.zero_()

    def forward(self, x):
        with torch.no_grad():
            feat = self.net(x)
        return self.fc(feat)


def knn_predict(feature, feature_bank, feature_labels, classes, knn_k, knn_t):
    """A common way of evaluating self-supervised learning.

        This code was taken and adapted from here:
        # knn monitor as in InstDisc https://arxiv.org/abs/1805.01978
        # implementation follows http://github.com/zhirongw/lemniscate.pytorch and https://github.com/leftthomas/SimCLR

        Args:
            feature:
                Features of the current batch size in the test set
            feature_bank:
                Features of all samples in the training set
            feature_labels:
                The labels of all samples in the training set, corresponding to feature_bank.
            classes:
                Total number of classes in the dataset
            knn_k:
                Number of top-k neighbors
            knn_t:
                The weights of the KNN

        Returns:
            pred_labels:
                Labels predicted by the current test sample
    """
    # compute cos similarity between each feature vector and feature bank ---> [B, N]
    sim_matrix = torch.mm(feature, feature_bank)
    # [B, K]
    sim_weight, sim_indices = sim_matrix.topk(k=knn_k, dim=-1)
    # [B, K]
    sim_labels = torch.gather(feature_labels.expand(feature.size(0), -1), dim=-1, index=sim_indices)
    sim_weight = (sim_weight / knn_t).exp()

    # counts for each class
    one_hot_label = torch.zeros(feature.size(0) * knn_k, classes, device=sim_labels.device)
    # [B*K, C]
    one_hot_label = one_hot_label.scatter(dim=-1, index=sim_labels.view(-1, 1), value=1.0)
    # weighted score ---> [B, C]
    pred_scores = torch.sum(one_hot_label.view(feature.size(0), -1, classes) * sim_weight.unsqueeze(dim=-1), dim=1)

    pred_labels = pred_scores.argsort(dim=-1, descending=True)
    return pred_labels


def rand_bbox(size, lam):
    """Used for cutmix to mix two images, to determine the range of images to be mixed.

    This code was taken and adapted from here:
    https://github.com/haohang96/bingo/blob/1c632fc37c5d22225d3af70bd85172192504d62e/utils.py#L20.

    Args:
        size:
            The size of the original image.
        lam:
            The scale of the original image as a proportion of the mixed image.
            lam takes the value in the range [0, 1].
    Returns:
        bbx1, bby1, bbx2, bby2:
            four quadrants of the mixing area
    """
    W = size[2]
    H = size[3]
    cut_rat = np.sqrt(1. - lam)
    cut_w = np.int(W * cut_rat)
    cut_h = np.int(H * cut_rat)

    # uniform
    cx = np.random.randint(W)
    cy = np.random.randint(H)

    bbx1 = np.clip(cx - cut_w // 2, 0, W)
    bby1 = np.clip(cy - cut_h // 2, 0, H)
    bbx2 = np.clip(cx + cut_w // 2, 0, W)
    bby2 = np.clip(cy + cut_h // 2, 0, H)

    return bbx1, bby1, bbx2, bby2

# come from ReSSL
def setup_seed(seed):
    """Used to set a random seed to keep the parameter seed consistent during reproduction.

    Args:
        seed:
            random number
    """
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

@torch.no_grad()
def batch_shuffle(
    batch: torch.Tensor, distributed: bool = False
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Randomly shuffles all tensors in the batch.

    Args:
        batch:
            The batch to shuffle.
        distributed:
            If True then batches are shuffled across multiple gpus.

    Returns:
        A (batch, shuffle) tuple where batch is the shuffled version of the
        input batch and shuffle is an index to restore the original order.

    Examples:
        >>> # forward pass through the momentum model with batch shuffling
        >>> x1_shuffled, shuffle = batch_shuffle(x1)
        >>> f1 = moco_momentum(x1)
        >>> out0 = projection_head_momentum(f0)
        >>> out1 = batch_unshuffle(out1, shuffle)
    """
    if distributed:
        return batch_shuffle_distributed(batch)
    batch_size = batch.shape[0]
    shuffle = torch.randperm(batch_size, device=batch.device)
    return batch[shuffle], shuffle


@torch.no_grad()
def batch_unshuffle(
    batch: torch.Tensor,
    shuffle: torch.Tensor,
    distributed: bool = False,
) -> torch.Tensor:
    """Unshuffles a batch.

    Args:
        batch:
            The batch to unshuffle.
        shuffle:
            Index to unshuffle the batch.
        distributed:
            If True then the batch is unshuffled across multiple gpus.

    Returns:
        The unshuffled batch.

    Examples:
        >>> # forward pass through the momentum model with batch shuffling
        >>> x1_shuffled, shuffle = batch_shuffle(x1)
        >>> f1 = moco_momentum(x1)
        >>> out0 = projection_head_momentum(f0)
        >>> out1 = batch_unshuffle(out1, shuffle)
    """
    if distributed:
        return batch_unshuffle_distributed(batch, shuffle)
    unshuffle = torch.argsort(shuffle)
    return batch[unshuffle]


@torch.no_grad()
def concat_all_gather(x: torch.Tensor) -> torch.Tensor:
    """Returns concatenated instances of x gathered from all gpus.

    This code was taken and adapted from here:
    https://github.com/facebookresearch/moco.

    """
    output = [torch.empty_like(x) for _ in range(dist.get_world_size())]
    dist.all_gather(output, x, async_op=False)
    output = torch.cat(output, dim=0)
    return output


@torch.no_grad()
def batch_shuffle_distributed(batch: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
    """Shuffles batch over multiple gpus.

    This code was taken and adapted from here:
    https://github.com/facebookresearch/moco.

    Args:
        batch:
            The tensor to shuffle.

    Returns:
        A (batch, shuffle) tuple where batch is the shuffled version of the
        input batch and shuffle is an index to restore the original order.

    """
    # gather from all gpus
    batch_size_this = batch.shape[0]
    batch_gather = concat_all_gather(batch)
    batch_size_all = batch_gather.shape[0]

    num_gpus = batch_size_all // batch_size_this

    # random shuffle index
    idx_shuffle = torch.randperm(batch_size_all).cuda()

    # broadcast to all gpus
    dist.broadcast(idx_shuffle, src=0)

    # index for restoring
    shuffle = torch.argsort(idx_shuffle)

    # shuffled index for this gpu
    gpu_idx = dist.get_rank()
    idx_this = idx_shuffle.view(num_gpus, -1)[gpu_idx]

    return batch_gather[idx_this], shuffle


@torch.no_grad()
def batch_unshuffle_distributed(
    batch: torch.Tensor, shuffle: torch.Tensor
) -> torch.Tensor:
    """Undo batch shuffle over multiple gpus.

    This code was taken and adapted from here:
    https://github.com/facebookresearch/moco.

    Args:
        batch:
            The tensor to unshuffle.
        shuffle:
            Index to restore the original tensor.

    Returns:
        The unshuffled tensor.

    """
    # gather from all gpus
    batch_size_this = batch.shape[0]
    batch_gather = concat_all_gather(batch)
    batch_size_all = batch_gather.shape[0]

    num_gpus = batch_size_all // batch_size_this

    # restored index for this gpu
    gpu_idx = dist.get_rank()
    idx_this = shuffle.view(num_gpus, -1)[gpu_idx]

    return batch_gather[idx_this]


def deactivate_requires_grad(model: nn.Module):
    """Deactivates the requires_grad flag for all parameters of a model.

    This has the same effect as permanently executing the model within a `torch.no_grad()`
    context. Use this method to disable gradient computation and therefore
    training for a model.

    Examples:
        >>> backbone = resnet18()
        >>> deactivate_requires_grad(backbone)
    """
    for param in model.parameters():
        param.requires_grad = False


def activate_requires_grad(model: nn.Module):
    """Activates the requires_grad flag for all parameters of a model.

    Use this method to activate gradients for a model (e.g. after deactivating
    them using `deactivate_requires_grad(...)`).

    Examples:
        >>> backbone = resnet18()
        >>> activate_requires_grad(backbone)
    """
    for param in model.parameters():
        param.requires_grad = True


def update_momentum(model: nn.Module, model_ema: nn.Module, m: float):
    """Updates parameters of `model_ema` with Exponential Moving Average of `model`

    Momentum encoders are a crucial component fo models such as MoCo or BYOL.

    Examples:
        >>> backbone = resnet18()
        >>> projection_head = MoCoProjectionHead()
        >>> backbone_momentum = copy.deepcopy(moco)
        >>> projection_head_momentum = copy.deepcopy(projection_head)
        >>>
        >>> # update momentum
        >>> update_momentum(moco, moco_momentum, m=0.999)
        >>> update_momentum(projection_head, projection_head_momentum, m=0.999)
    """
    for model_ema, model in zip(model_ema.parameters(), model.parameters()):
        model_ema.data = model_ema.data * m + model.data * (1.0 - m)


@torch.no_grad()
def normalize_weight(weight: nn.Parameter, dim: int = 1, keepdim: bool = True):
    """Normalizes the weight to unit length along the specified dimension."""
    weight.div_(torch.norm(weight, dim=dim, keepdim=keepdim))


# copy paste from PyTorch master branch as it is not available in older releases
# source: https://github.com/pytorch/pytorch/blob/20ac7362009dd8e0aca6e72fc9357773136a83b8/torch/nn/init.py#L22-L54
def _no_grad_trunc_normal(
    tensor: torch.Tensor,
    mean: float,
    std: float,
    a: float,
    b: float,
) -> torch.Tensor:
    """Initializes the input tensor with a truncated normal distribution.

    This method is based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf

    Args:
        tensor:
            The tensor to initialize.
        mean:
            Mean of the distribution.
        std:
            Standard deviation of the distribution.
        a:
            Minimum value of the distribution, values below will be clamped.
        b:
            Maximum value of the distribution, values above will be clamped.

    """

    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn(
            "mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
            "The distribution of values may be incorrect.",
            stacklevel=2,
        )

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.0))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor

# DINO
def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # type: (Tensor, float, float, float, float) -> Tensor
    return _no_grad_trunc_normal(tensor, mean, std, a, b)


# DINO
def cosine_scheduler(base_value, final_value, epochs, niter_per_ep, warmup_epochs=0, start_warmup_value=0):
    '''
    start_warmup_value(0) ------ linear warm up -------> base_value    if warmup_epochs > 0
    base_value            ------ cosine schedule ------> final_value

    special case:
        if base_value == final_value
                start_warmup_value(0) ------ linear warm up -------> base_value    if warmup_epochs > 0
                keep constants
    '''
    warmup_schedule = np.array([])
    warmup_iters = warmup_epochs * niter_per_ep
    if warmup_epochs > 0:
        warmup_schedule = np.linspace(start_warmup_value, base_value, warmup_iters)

    iters = np.arange(epochs * niter_per_ep - warmup_iters)
    schedule = final_value + 0.5 * (base_value - final_value) * (1 + np.cos(np.pi * iters / len(iters)))

    schedule = np.concatenate((warmup_schedule, schedule))
    assert len(schedule) == epochs * niter_per_ep
    return schedule


# DINO
def multicropwrapper(backbone, head, x, prediction=None):
    """
    Perform forward pass separately on each resolution input.
    The inputs corresponding to a single resolution are clubbed and single
    forward is run on the same resolution inputs. Hence we do several
    forward passes = number of different resolutions used. We then
    concatenate all the output features and run the head forward on these
    concatenated features.
    """
    # convert to list
    # x                 : [2, 2, 3, 3, 3, 4, 5]
    if not isinstance(x, list):
        x = [x]
    # unique_consecutive: (tensor([2, 3, 4, 5]), tensor([2, 3, 1, 1]))
    # cumsum            : tensor([2, 5, 6, 7])
    idx_crops = torch.cumsum(torch.unique_consecutive(
        torch.tensor([inp.shape[-1] for inp in x]),
        return_counts=True,
    )[1], 0)
    start_idx, output = 0, torch.empty(0).to(x[0].device)
    for end_idx in idx_crops:
        _out = backbone(torch.cat(x[start_idx: end_idx]))
        # The output is a tuple with XCiT model. See:
        # https://github.com/facebookresearch/xcit/blob/master/xcit.py#L404-L405
        if isinstance(_out, tuple):
            _out = _out[0]
        # accumulate outputs
        output = torch.cat((output, _out))
        start_idx = end_idx
    # Run the head forward on the concatenated features.
    if prediction != None:
        return prediction(head(output))
    return head(output)


# DINO
def get_params_groups(net, head):
    regularized = []
    not_regularized = []
    for name, param in net.named_parameters():
        if not param.requires_grad:
            continue
        # we do not regularize biases nor Norm parameters
        if name.endswith(".bias") or len(param.shape) == 1:
            not_regularized.append(param)
        else:
            regularized.append(param)

    for name, param in head.named_parameters():
        if not param.requires_grad:
            continue
        # we do not regularize biases nor Norm parameters
        if name.endswith(".bias") or len(param.shape) == 1:
            not_regularized.append(param)
        else:
            regularized.append(param)
    return [{'params': regularized}, {'params': not_regularized, 'weight_decay': 0.}]


def repeat_token(token: torch.Tensor, size: Tuple[int, int]) -> torch.Tensor:
    """Repeats a token size times.

    Args:
        token:
            Token tensor with shape (1, 1, dim).
        size:
            (batch_size, sequence_length) tuple.

    Returns:
        Tensor with shape (batch_size, sequence_length, dim) containing copies
        of the input token.

    """
    batch_size, sequence_length = size
    return token.repeat(batch_size, sequence_length, 1)


def expand_index_like(index: torch.Tensor, tokens: torch.Tensor) -> torch.Tensor:
    """Expands the index along the last dimension of the input tokens.

    Args:
        index:
            Index tensor with shape (batch_size, idx_length) where each entry is
            an index in [0, sequence_length).
        tokens:
            Tokens tensor with shape (batch_size, sequence_length, dim).

    Returns:
        Index tensor with shape (batch_size, idx_length, dim) where the original
        indices are repeated dim times along the last dimension.

    """
    dim = tokens.shape[-1]
    index = index.unsqueeze(-1).expand(-1, -1, dim)
    return index


def get_at_index(tokens: torch.Tensor, index: torch.Tensor) -> torch.Tensor:
    """Selects tokens at index.

    Args:
        tokens:
            Token tensor with shape (batch_size, sequence_length, dim).
        index:
            Index tensor with shape (batch_size, index_length) where each entry is
            an index in [0, sequence_length).

    Returns:
        Token tensor with shape (batch_size, index_length, dim) containing the
        selected tokens.

    """
    index = expand_index_like(index, tokens)
    return torch.gather(tokens, 1, index)


def set_at_index(
    tokens: torch.Tensor, index: torch.Tensor, value: torch.Tensor
) -> torch.Tensor:
    """Copies all values into the input tensor at the given indices.

    Args:
        tokens:
            Tokens tensor with shape (batch_size, sequence_length, dim).
        index:
            Index tensor with shape (batch_size, index_length).
        value:
            Value tensor with shape (batch_size, index_length, dim).

    Returns:
        Tokens tensor with shape (batch_size, sequence_length, dim) containing
        the new values.

    """
    index = expand_index_like(index, tokens)
    return torch.scatter(tokens, 1, index, value)


def mask_at_index(
    tokens: torch.Tensor, index: torch.Tensor, mask_token: torch.Tensor
) -> torch.Tensor:
    """Copies mask token into the input tensor at the given indices.

    Args:
        tokens:
            Tokens tensor with shape (batch_size, sequence_length, dim).
        index:
            Index tensor with shape (batch_size, index_length).
        mask_token:
            Value tensor with shape (1, 1, dim).

    Returns:
        Tokens tensor with shape (batch_size, sequence_length, dim) containing
        the new values.

    """
    mask = tokens.new_zeros(tokens.shape)
    mask = set_at_index(mask, index, 1)
    return (1 - mask) * tokens + mask * mask_token


def prepend_class_token(
    tokens: torch.Tensor, class_token: torch.Tensor
) -> torch.Tensor:
    """Prepends class token to tokens.

    Args:
        tokens:
            Tokens tensor with shape (batch_size, sequence_length, dim).
        class_token:
            Class token with shape (1, 1, dim).

    Returns:
        Tokens tensor with the class token prepended at index 0 in every
        sequence. The tensor has shape (batch_size, sequence_length + 1, dim).
    """
    batch_size = tokens.shape[0]
    batch_class_token = class_token.expand(batch_size, -1, -1)
    return torch.cat([batch_class_token, tokens], dim=1)


def patchify(images: torch.Tensor, patch_size: int) -> torch.Tensor:
    """Converts a batch of input images into patches.

    Args:
        images:
            Images tensor with shape (batch_size, channels, height, width)
        patch_size:
            Patch size in pixels. Image width and height must be multiples of
            the patch size.

    Returns:
        Patches tensor with shape (batch_size, num_patches, channels * patch_size ** 2)
        where num_patches = image_width / patch_size * image_height / patch_size.

    """
    # N, C, H, W = (batch_size, channels, height, width)
    N, C, H, W = images.shape
    assert H == W and H % patch_size == 0

    patch_h = patch_w = H // patch_size
    num_patches = patch_h * patch_w
    patches = images.reshape(shape=(N, C, patch_h, patch_size, patch_w, patch_size))
    patches = torch.einsum("nchpwq->nhwpqc", patches)
    patches = patches.reshape(shape=(N, num_patches, patch_size**2 * C))
    return patches


def random_token_mask(
    size: Tuple[int, int],
    mask_ratio: float = 0.6,
    mask_class_token: bool = False,
    device: Optional[Union[torch.device, str]] = None,
) -> torch.Tensor:
    """Creates random token masks.

    Args:
        size:
            Size of the token batch for which to generate masks.
            Should be (batch_size, sequence_length).
        mask_ratio:
            Percentage of tokens to mask.
        mask_class_token:
            If False the class token is never masked. If True the class token
            might be masked.
        device:
            Device on which to create the index masks.

    Returns:
        A (index_keep, index_mask) tuple where each index is a tensor.
        index_keep contains the indices of the unmasked tokens and has shape
        (batch_size, num_keep). index_mask contains the indices of the masked
        tokens and has shape (batch_size, sequence_length - num_keep).
        num_keep is equal to sequence_length * (1- mask_ratio).

    """
    batch_size, sequence_length = size
    num_keep = int(sequence_length * (1 - mask_ratio))

    noise = torch.rand(batch_size, sequence_length, device=device)
    if not mask_class_token and sequence_length > 0:
        # make sure that class token is not masked
        noise[:, 0] = -1
        num_keep = max(1, num_keep)

    # get indices of tokens to keep
    indices = torch.argsort(noise, dim=1)
    idx_keep = indices[:, :num_keep]
    idx_mask = indices[:, num_keep:]

    return idx_keep, idx_mask


def nearest_neighbors(
    input_maps: torch.Tensor,
    candidate_maps: torch.Tensor,
    distances: torch.Tensor,
    num_matches: int,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Finds the nearest neighbors of the maps in input_maps in candidate_maps.

    Args:
        input_maps:
            A tensor of maps for which to find nearest neighbors.
            It has size: [batch_size, input_map_size, feature_dimension]
        candidate_maps:
            A tensor of maps to search for nearest neighbors.
            It has size: [batch_size, candidate_map_size, feature_dimension]
        distances:
            A tensor of distances between the maps in input_maps and candidate_maps.
            It has size: [batch_size, input_map_size, candidate_map_size]
        num_matches:
            Number of nearest neighbors to return. If num_matches is None or -1,
            all the maps in candidate_maps are considered.

    Returns:
        A tuple of tensors, containing the nearest neighbors in input_maps and candidate_maps.
        They both have size: [batch_size, input_map_size, feature_dimension]
    """

    if num_matches is None or num_matches == -1 or num_matches > input_maps.size(1):
        num_matches = input_maps.size(1)

    # Find nearest neighbour of each input element in the candidate map
    topk_values, topk_indices = distances.topk(
        k=1, dim=2, largest=False
    )  # [bsz, input_map_size, 1]
    topk_values = topk_values.squeeze(-1)  # [bsz, input_map_size]

    # Select num_matches neighbors pairs having the lowest distance value.
    _, min_indices = topk_values.topk(
        k=num_matches, dim=1, largest=False
    )  # [bsz, num_matches]

    # Create the filtered input map with num_matches lowest distance values.
    feature_dimension = input_maps.shape[2]
    filtered_input_maps = torch.gather(
        input_maps, 1, min_indices.unsqueeze(-1).expand(-1, -1, feature_dimension)
    )  # [bsz, num_matches, feature_dimension]

    # Create candidate maps in the same way as input maps, but using corrispondent candidate values
    selected_candidate_maps = torch.gather(
        candidate_maps, 1, topk_indices.expand(-1, -1, feature_dimension)
    )  # [bsz, input_map_size, feature_dimension]
    filtered_candidate_maps = torch.gather(
        selected_candidate_maps,
        1,
        min_indices.unsqueeze(-1).expand(-1, -1, feature_dimension),
    )  # [bsz, num_matches, feature_dimension]

    return filtered_input_maps, filtered_candidate_maps


def get_weight_decay_parameters(
    modules: Iterable[Module],
    decay_batch_norm: bool = False,
    decay_bias: bool = False,
) -> Tuple[List[Parameter], List[Parameter]]:
    """Returns all parameters of the modules that should be decayed and not decayed.

    Args:
        modules:
            List of modules to get the parameters from.
        no_batch_norm:
            If True, batch norm parameters are decayed.
        no_bias:
            If True, bias parameters are decayed.

    Returns:
        (params, params_no_weight_decay) tuple.
    """
    params = []
    params_no_weight_decay = []
    for module in modules:
        for mod in module.modules():
            if isinstance(mod, _BatchNorm):
                if not decay_batch_norm:
                    params_no_weight_decay.extend(mod.parameters(recurse=False))
                else:
                    params.extend(mod.parameters(recurse=False))
            else:
                for name, param in mod.named_parameters(recurse=False):
                    if not decay_bias and name.endswith("bias"):
                        params_no_weight_decay.append(param)
                    else:
                        params.append(param)
    return params, params_no_weight_decay
