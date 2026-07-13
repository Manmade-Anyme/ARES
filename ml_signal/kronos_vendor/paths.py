"""
Per-sample forecast paths for Kronos.

Upstream's KronosPredictor.predict() (model/kronos.py) internally draws
`sample_count` stochastic forecast paths but returns only their mean — see
ATTRIBUTION.md. A Monte-Carlo barrier-hit probability needs the individual
paths, so this reimplements the same autoregressive decode loop with the
final `np.mean(preds, axis=1)` step removed. Only the last few lines differ
from model.kronos.auto_regressive_inference; everything else (tokenizer
encode/decode, model.decode_s1/s2, sampling) is the unmodified vendored code.
"""

import numpy as np
import pandas as pd
import torch

from model.kronos import KronosPredictor, calc_time_stamps, sample_from_logits


def _autoregressive_paths(tokenizer, model, x, x_stamp, y_stamp, max_context,
                           pred_len, clip, T, top_k, top_p, sample_count, verbose):
    with torch.no_grad():
        x = torch.clip(x, -clip, clip)
        device = x.device
        x = x.unsqueeze(1).repeat(1, sample_count, 1, 1).reshape(-1, x.size(1), x.size(2)).to(device)
        x_stamp = x_stamp.unsqueeze(1).repeat(1, sample_count, 1, 1).reshape(-1, x_stamp.size(1), x_stamp.size(2)).to(device)
        y_stamp = y_stamp.unsqueeze(1).repeat(1, sample_count, 1, 1).reshape(-1, y_stamp.size(1), y_stamp.size(2)).to(device)

        x_token = tokenizer.encode(x, half=True)
        initial_seq_len = x.size(1)
        batch_size = x_token[0].size(0)
        total_seq_len = initial_seq_len + pred_len
        full_stamp = torch.cat([x_stamp, y_stamp], dim=1)

        generated_pre = x_token[0].new_empty(batch_size, pred_len)
        generated_post = x_token[1].new_empty(batch_size, pred_len)

        pre_buffer = x_token[0].new_zeros(batch_size, max_context)
        post_buffer = x_token[1].new_zeros(batch_size, max_context)
        buffer_len = min(initial_seq_len, max_context)
        if buffer_len > 0:
            start_idx = max(0, initial_seq_len - max_context)
            pre_buffer[:, :buffer_len] = x_token[0][:, start_idx:start_idx + buffer_len]
            post_buffer[:, :buffer_len] = x_token[1][:, start_idx:start_idx + buffer_len]

        for i in range(pred_len):
            current_seq_len = initial_seq_len + i
            window_len = min(current_seq_len, max_context)

            if current_seq_len <= max_context:
                input_tokens = [pre_buffer[:, :window_len], post_buffer[:, :window_len]]
            else:
                input_tokens = [pre_buffer, post_buffer]

            context_end = current_seq_len
            context_start = max(0, context_end - max_context)
            current_stamp = full_stamp[:, context_start:context_end, :].contiguous()

            s1_logits, context = model.decode_s1(input_tokens[0], input_tokens[1], current_stamp)
            s1_logits = s1_logits[:, -1, :]
            sample_pre = sample_from_logits(s1_logits, temperature=T, top_k=top_k, top_p=top_p, sample_logits=True)

            s2_logits = model.decode_s2(context, sample_pre)
            s2_logits = s2_logits[:, -1, :]
            sample_post = sample_from_logits(s2_logits, temperature=T, top_k=top_k, top_p=top_p, sample_logits=True)

            generated_pre[:, i] = sample_pre.squeeze(-1)
            generated_post[:, i] = sample_post.squeeze(-1)

            if current_seq_len < max_context:
                pre_buffer[:, current_seq_len] = sample_pre.squeeze(-1)
                post_buffer[:, current_seq_len] = sample_post.squeeze(-1)
            else:
                pre_buffer.copy_(torch.roll(pre_buffer, shifts=-1, dims=1))
                post_buffer.copy_(torch.roll(post_buffer, shifts=-1, dims=1))
                pre_buffer[:, -1] = sample_pre.squeeze(-1)
                post_buffer[:, -1] = sample_post.squeeze(-1)

        full_pre = torch.cat([x_token[0], generated_pre], dim=1)
        full_post = torch.cat([x_token[1], generated_post], dim=1)

        context_start = max(0, total_seq_len - max_context)
        input_tokens = [
            full_pre[:, context_start:total_seq_len].contiguous(),
            full_post[:, context_start:total_seq_len].contiguous(),
        ]
        z = tokenizer.decode(input_tokens, half=True)
        z = z.reshape(-1, sample_count, z.size(1), z.size(2))
        preds = z.cpu().numpy()
        # NOTE: no np.mean here — this is the one line removed vs. upstream.
        return preds[:, :, -pred_len:, :]


def predict_paths(predictor: KronosPredictor, df, x_timestamp, y_timestamp, pred_len,
                   T=1.0, top_k=0, top_p=0.9, sample_count=20, verbose=False):
    """
    Like KronosPredictor.predict(), but returns `sample_count` individual
    forecast paths instead of their mean.

    Returns a list of `sample_count` DataFrames, each with columns
    ['open','high','low','close','volume','amount'], indexed by y_timestamp.
    """
    if not isinstance(df, pd.DataFrame):
        raise ValueError("Input must be a pandas DataFrame.")
    price_cols = predictor.price_cols
    vol_col, amt_col = predictor.vol_col, predictor.amt_vol

    if not all(c in df.columns for c in price_cols):
        raise ValueError(f"Price columns {price_cols} not found in DataFrame.")

    df = df.copy()
    if vol_col not in df.columns:
        df[vol_col] = 0.0
        df[amt_col] = 0.0
    if amt_col not in df.columns:
        df[amt_col] = df[vol_col] * df[price_cols].mean(axis=1)

    if df[price_cols + [vol_col, amt_col]].isnull().values.any():
        raise ValueError("Input DataFrame contains NaN values in price or volume columns.")

    x_stamp_df = calc_time_stamps(x_timestamp)
    y_stamp_df = calc_time_stamps(y_timestamp)

    x = df[price_cols + [vol_col, amt_col]].values.astype(np.float32)
    x_stamp = x_stamp_df.values.astype(np.float32)
    y_stamp = y_stamp_df.values.astype(np.float32)

    x_mean, x_std = np.mean(x, axis=0), np.std(x, axis=0)
    x_norm = np.clip((x - x_mean) / (x_std + 1e-5), -predictor.clip, predictor.clip)

    x_norm = x_norm[np.newaxis, :]
    x_stamp = x_stamp[np.newaxis, :]
    y_stamp = y_stamp[np.newaxis, :]

    x_tensor = torch.from_numpy(x_norm.astype(np.float32)).to(predictor.device)
    x_stamp_tensor = torch.from_numpy(x_stamp.astype(np.float32)).to(predictor.device)
    y_stamp_tensor = torch.from_numpy(y_stamp.astype(np.float32)).to(predictor.device)

    preds = _autoregressive_paths(
        predictor.tokenizer, predictor.model, x_tensor, x_stamp_tensor, y_stamp_tensor,
        predictor.max_context, pred_len, predictor.clip, T, top_k, top_p, sample_count, verbose,
    )
    preds = preds[0]  # drop batch dim (batch size is always 1 here)
    preds = preds * (x_std + 1e-5) + x_mean

    return [
        pd.DataFrame(preds[k], columns=price_cols + [vol_col, amt_col], index=y_timestamp)
        for k in range(sample_count)
    ]
