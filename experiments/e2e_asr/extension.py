from typing import Tuple, Optional, Dict

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor

from sru import SRUCell


class SRUppTransducerAttention(nn.Module):
    """
    Self-attention module used in SRU++ module.
    """

    __constants__ = ['in_features', 'out_features', 'proj_features', 'num_heads',
                     'attn_dropout', 'rezero_init_alpha', 'right_window']

    def __init__(self,
                 in_features: int,
                 out_features: int,
                 proj_features: int,
                 num_heads: int = 1,
                 dropout: float = 0.0,
                 attn_dropout: float = 0.0,
                 rezero_init_alpha: float = 0.0,
                 layer_norm: bool = False,
                 normalize_after: bool = True,
                 right_window: int = 0):
        """Initialize the self-attention module.

        Parameters
        ----------
        in_features: int
            the number of input features.
        out_features: int
            the number of output features.
        proj_features: int
            the number of features used for attention computation. The input is projected into
            this dimension first. After that the module apply the query-key-value attention
            computation. The output is projected to dimension `out_features`.
        num_heads: int, optional
            the number of attention heads used. `proj_features` must be multipler of this value
            (default=1).
        dropout: float, optional
            dropout probability applied after attention computation and before the final projection
            (default=0.0).
        attn_dropout: float, optional
            dropout probability applied on attention map.
        rezero_init_alpha: float, optional
            initial scalar value for the attention transformation `x + alpha * Attention(x)`
            (default=0).
        normalize_after: bool, optional
            if True, apply post layer normalization; otherwise apply pre layer normalization
            (default=True).

        """
        super(SRUppTransducerAttention, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.proj_features = proj_features
        self.num_heads = num_heads
        self.right_window = right_window
        self.dropout = nn.Dropout(dropout)
        self.attn_dropout = float(attn_dropout)
        self.rezero_init_alpha = float(rezero_init_alpha)
        self.linear1 = nn.Linear(in_features, proj_features, bias=False)
        self.linear2 = nn.Linear(proj_features, proj_features * 2, bias=False)
        self.linear3 = nn.Linear(proj_features, out_features, bias=False)
        self.alpha = nn.Parameter(torch.Tensor([float(rezero_init_alpha)]))  # type: ignore
        self.normalize_after = normalize_after
        self.layer_norm: Optional[nn.Module] = None
        if layer_norm:
            self.layer_norm = nn.LayerNorm(proj_features)

        if proj_features % num_heads != 0:
            raise ValueError("proj_features ({}) must be divisible by num_heads ({})".format(
                proj_features, num_heads
            ))
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.xavier_uniform_(self.linear1.weight)
        nn.init.xavier_uniform_(self.linear2.weight)
        nn.init.xavier_uniform_(self.linear3.weight)
        self.alpha.data[:] = self.rezero_init_alpha
        if self.linear1.bias is not None:
            self.linear1.bias.data.zero_()
        if self.linear2.bias is not None:
            self.linear2.bias.data.zero_()
        if self.linear3.bias is not None:
            self.linear3.bias.data.zero_()
        if self.dropout.p > 0:
            self.linear3.weight.data.mul_((1 - self.dropout.p)**0.5)

    def forward(self,
                input: Tensor,
                mask_pad: Optional[Tensor] = None,
                attn_mask: Optional[Tensor] = None,
                incremental_state: Optional[Dict[str, Dict[str, Optional[Tensor]]]] = None,
                ) -> Tuple[Tensor, Optional[Dict[str, Dict[str, Optional[Tensor]]]]]:
        """The forward method of SRU++ attention.
        """

        src_len = tgt_len = input.size(0)
        bsz = input.size(1)
        proj_dim = self.proj_features
        num_heads = self.num_heads
        head_dim = proj_dim // num_heads
        scaling = float(head_dim) ** -0.5

        q = residual = self.linear1(input)

        # take saved queries in the state
        if incremental_state is not None:
            # during inference mask_pad should not be set
            assert mask_pad is None
            right_window = self.right_window

            # fetch previously computed queries
            if "attn_state" in incremental_state:
                state = incremental_state["attn_state"]
            else:
                state = {}
            if "saved_query" in state:
                saved_query = state["saved_query"]
                assert saved_query is not None
                all_query = torch.cat([saved_query, q], dim=0)
            else:
                all_query = q

            # number of queries that are ready for the attention forward
            num_query_ready = all_query.size(0) - right_window
            if num_query_ready < 0:
                num_query_ready = 0
            query_ready = all_query[:num_query_ready]
            query_not_ready = all_query[num_query_ready:]

            # update q, tgt_len and attn_mask
            tgt_len = query_ready.size(0)
            q = residual = query_ready
            if attn_mask is not None:
                start_idx = attn_mask.size(0) - all_query.size(0)
                end_idx = attn_mask.size(0) - query_not_ready.size(0)
                attn_mask = attn_mask[start_idx:end_idx]

        # pre-layernorm
        if self.layer_norm is not None and not self.normalize_after:
            q = self.layer_norm(q)

        # key, value
        k, v = self.linear2(q).chunk(2, dim=-1)
        k = k.contiguous()
        v = v.contiguous()

        # take saved keys and values in the state and
        # update the state with new queries, keys and values
        if incremental_state is not None:
            if "saved_key" in state:
                saved_key = state["saved_key"]
                assert saved_key is not None
                k = torch.cat([saved_key, k], dim=0)
            if "saved_value" in state:
                saved_value = state["saved_value"]
                assert saved_value is not None
                v = torch.cat([saved_value, v], dim=0)
            src_len = k.size(0)
            assert v.size(0) == k.size(0)

            state["saved_query"] = query_not_ready
            state["saved_key"] = k
            state["saved_value"] = v
            incremental_state["attn_state"] = state

        q = q.contiguous().view(tgt_len, -1, head_dim).transpose(0, 1)
        k = k.contiguous().view(src_len, -1, head_dim).transpose(0, 1)
        v = v.contiguous().view(src_len, -1, head_dim).transpose(0, 1)

        # (bsz * num_heads, tgt_len, src_len)
        q = q * scaling
        attn_output_weights = torch.bmm(q, k.transpose(1, 2))
        assert list(attn_output_weights.size()) == [bsz * num_heads, tgt_len, src_len]

        if attn_mask is not None:
            if list(attn_mask.size()) != [tgt_len, src_len]:
                raise ValueError("attn_mask has size {} but expect {}.".format(
                    list(attn_mask.size()),
                    [tgt_len, src_len]
                ))
            attn_output_weights += attn_mask.unsqueeze(0)

        if mask_pad is not None:
            if list(mask_pad.size()) != [src_len, bsz]:
                raise ValueError("mask_pad has size {} but expect {}.".format(
                    list(mask_pad.size()),
                    [src_len, bsz]
                ))
            attn_output_weights = attn_output_weights.view(bsz, num_heads, tgt_len, src_len)
            attn_output_weights = attn_output_weights.masked_fill(
                mask_pad.transpose(0, 1).unsqueeze(1).unsqueeze(2),  # (bsz, 1, 1, src_len)
                float('-inf'),
            )
            attn_output_weights = attn_output_weights.view(bsz * num_heads, tgt_len, src_len)

        attn_output_weights = F.softmax(attn_output_weights, dim=-1)
        attn_output_weights = F.dropout(attn_output_weights, p=self.attn_dropout,
                                        training=self.training)

        # (bsz * num_heads, tgt_len, src_len) x (bsz * num_heads, src_len, head_dim)
        #     ---->  (bsz * num_heads, tgt_len, head_dim)
        attn_output = torch.bmm(attn_output_weights, v)
        attn_output = attn_output.transpose(0, 1).contiguous().view(tgt_len, bsz, proj_dim)

        attn_output = attn_output * self.alpha + residual
        if self.normalize_after and self.layer_norm is not None:
            attn_output = self.layer_norm(attn_output)

        # (tgt_len, bsz, out_dim)
        attn_output = self.linear3(self.dropout(attn_output))
        return attn_output, incremental_state


class SRUppTransducerLinear(nn.Module):
    """
    Projected linear module used in SRU++ Transducer module.
    """

    __constants__ = ['in_features', 'out_features', 'proj_features', 'right_window']

    def __init__(self,
                 in_features: int,
                 out_features: int,
                 proj_features: int,
                 dropout: float = 0.0,
                 layer_norm: bool = False,
                 right_window: int = 0):
        """Initialize the projected linear module.

        Parameters
        ----------
        in_features: int
            the number of input features.
        out_features: int
            the number of output features.
        proj_features: int
            the number of features used for attention computation. The input is projected into
            this dimension first. After that the module apply the query-key-value attention
            computation. The output is projected to dimension `out_features`.
        dropout: float, optional
            dropout probability applied after attention computation and before the final projection
            (default=0.0).
        layer_norm: bool, optional
            whether to apply layer normalization within the projected linear module.
        right_window: int, optional
            the size of the look-right window.
        """
        super(SRUppTransducerLinear, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.proj_features = proj_features
        self.right_window = right_window
        self.dropout = nn.Dropout(dropout)
        self.linear1 = nn.Linear(in_features, proj_features, bias=False)
        self.linear2 = nn.Linear(proj_features, out_features, bias=False)
        self.layer_norm: Optional[nn.Module] = None
        if layer_norm:
            self.layer_norm = nn.LayerNorm(proj_features)

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.xavier_uniform_(self.linear1.weight)
        nn.init.xavier_uniform_(self.linear2.weight)
        if self.linear1.bias is not None:
            self.linear1.bias.data.zero_()
        if self.linear2.bias is not None:
            self.linear2.bias.data.zero_()
        if self.dropout.p > 0:
            self.linear2.weight.data.mul_((1 - self.dropout.p)**0.5)

    def forward(self,
                input: Tensor,
                mask_pad: Optional[Tensor] = None,
                attn_mask: Optional[Tensor] = None,
                incremental_state: Optional[Dict[str, Dict[str, Optional[Tensor]]]] = None,
                ) -> Tuple[Tensor, Optional[Dict[str, Dict[str, Optional[Tensor]]]]]:
        """The forward method.
        """
        q = self.linear1(input)

        # take saved queries in the state
        if incremental_state is not None:
            # during inference mask_pad should not be set
            assert mask_pad is None
            right_window = self.right_window

            # fetch previously computed queries
            if "attn_state" in incremental_state:
                state = incremental_state["attn_state"]
            else:
                state = {}
            if "saved_query" in state:
                saved_query = state["saved_query"]
                assert saved_query is not None
                all_query = torch.cat([saved_query, q], dim=0)
            else:
                all_query = q

            # number of queries that are ready for the attention forward
            num_query_ready = all_query.size(0) - right_window
            if num_query_ready < 0:
                num_query_ready = 0
            query_ready = all_query[:num_query_ready]
            query_not_ready = all_query[num_query_ready:]
            q = query_ready

            # update state
            state["saved_query"] = query_not_ready
            incremental_state["attn_state"] = state

        if self.layer_norm is not None:
            q = self.layer_norm(q)
        output = self.linear2(self.dropout(q))
        return output, incremental_state


class SRUppTransducerCell(SRUCell):
    """
    A single layer of SRUpp-T module, inherited from SRUCell module
    """
    def __init__(self,
                 input_size: int,
                 hidden_size: int,
                 projection_size: int,
                 num_heads: int = 1,
                 dropout: float = 0.0,
                 attn_dropout: float = 0.0,
                 highway_bias: float = -2,
                 layer_norm: bool = True,
                 normalize_after: bool = True,
                 has_attention: bool = True,
                 right_window: int = 0):

        transform_module: Optional[nn.Module] = None
        if has_attention:
            transform_module = SRUppTransducerAttention(
                input_size,
                hidden_size,
                projection_size,
                num_heads=num_heads,
                dropout=dropout,
                attn_dropout=attn_dropout,
                layer_norm=layer_norm,
                normalize_after=normalize_after,
                right_window=right_window,
            )
        else:
            transform_module = SRUppTransducerLinear(
                input_size,
                hidden_size,
                projection_size,
                dropout=dropout,
                layer_norm=layer_norm,
                right_window=right_window,
            )
        super().__init__(input_size,
                         hidden_size,
                         dropout=dropout,
                         highway_bias=highway_bias,
                         transform_module=transform_module)

    def forward(self,
                input: Tensor,
                c0: Optional[Tensor] = None,
                mask_pad: Optional[Tensor] = None,
                attn_mask: Optional[Tensor] = None,
                incremental_state: Optional[Dict[str, Dict[str, Optional[Tensor]]]] = None,
                ) -> Tuple[Tensor, Tensor, Optional[Dict[str, Dict[str, Optional[Tensor]]]]]:
        """The forward method.
        """

        if input.dim() != 3:
            raise ValueError("Input must be 3 dimensional (length, bsz, d)")

        batch_size = input.size(-2)
        if c0 is None:
            c0 = torch.zeros(batch_size, self.output_size, dtype=input.dtype,
                             device=input.device)

        # get dropout mask
        mask_c: Optional[Tensor] = None
        if self.training and (self.dropout > 0):
            mask_c = self.get_dropout_mask_((batch_size, self.output_size),
                                            self.dropout)

        # compute U
        #   U is (length, batch_size, output_size * num_matrices)
        transform_module = self.transform_module
        U, incremental_state = transform_module(
            input,
            mask_pad=mask_pad,
            attn_mask=attn_mask,
            incremental_state=incremental_state
        )
        V = self.weight_c

        # if U is an empty tensor, we pop out empty tensor as well
        if U.numel() == 0:
            empty_h = torch.zeros(0, batch_size, self.output_size, dtype=input.dtype,
                                  device=input.device)
            return empty_h, c0, incremental_state
        else:
            # apply elementwise recurrence to get hidden states h and c
            h, c = self.apply_recurrence(U, V,
                                         input, c0,
                                         None,
                                         mask_c,
                                         mask_pad)
            return h, c, incremental_state