"""
This file implements model contamination detection through the sharded likelihood approach.
"""

import os
import random
import numpy as np
import torch
import math
import json
from scipy.stats import t as tdist
from tqdm import tqdm
from transformers import AutoTokenizer, AutoModelForCausalLM
from multiprocessing import Process, Queue


###### Code for Sharded Likelihood ######
def _load_dataset(dataset_path):
    # stringfy the dataset
    lines = []
    for i in range(len(dataset_path)):
        tmp_str = '{'
        for k, v in dataset_path[i].items():
            if type(v) == str:
                tmp_str += f'"{k}": "{v}",'
            else:
                tmp_str += f'"{k}": {v},'
        tmp_str = tmp_str[:-1] + '}'
        lines.append(tmp_str)

    return lines


def _compute_logprob_of_token_sequence(tokens, model, context_len=2048, stride=1024, device=0):
    """
    Approximates logp(tokens) by sliding a window over the tokens with a stride.
    """
    inputs  = tokens[:-1]
    targets = tokens[1:]

    logp = torch.zeros((1, 1), dtype=torch.float32).to(device)

    # compute the smallest multiple k of s so that t <= ks + c.
    t = len(inputs); c = context_len; s = stride
    k = math.ceil(max(0, t - c) / s)
    all_logps = []
    for j in range(k + 1):
        start    = s * j
        end      = min(s * j + c, t)
        rel_offs = max(0, c - s) if j > 0 else 0

        w_inp = inputs[start:end]; w_inp = torch.tensor(w_inp).to(device)
        w_trg = targets[start:end]; w_trg = torch.tensor(w_trg).to(device)

        model.eval()
        with torch.no_grad():
            out = model(torch.unsqueeze(w_inp, 0))
            logps = torch.nn.functional.log_softmax(out.logits[0], dim=-1)
            logps = logps.gather(-1, w_trg.unsqueeze(-1)).squeeze(-1)
            logp += logps[rel_offs:].sum()

        del w_inp
        del w_trg
        torch.cuda.empty_cache()

    return logp.item()


def _worker(
    model_name_or_path,
    context_len,
    stride,
    device,
    main_queue,
    worker_queue
):

    # Load model.
    m = AutoModelForCausalLM.from_pretrained(model_name_or_path)
    m.cuda(device)
    main_queue.put((device, True))

    # Wait for inference requests.
    while True:
        tokens, shard_id, is_canonical = worker_queue.get()

        if tokens == None: # Quit.
            break

        # Compute logprob of tokens.
        logprob = _compute_logprob_of_token_sequence(tokens,
                                                    m,
                                                    context_len,
                                                    stride,
                                                    device=device)

        # Send result to main process.
        main_queue.put((logprob, shard_id, is_canonical))

    del m

# Following the logic from this paper: https://arxiv.org/pdf/2310.17623.pdf
def main_sharded_likelihood(
    model_name_or_path,
    dataset_path,
    context_len=2048,
    stride=1024,
    num_shards=50,
    permutations_per_shard=250,
    random_seed=0,
    log_file_path=None,
    max_examples=5000
):
    os.environ['TOKENIZERS_PARALLELISM'] = "True"
    flatten = lambda l : [x for s in l for x in s]
    shuffle = lambda l : random.sample(l, k=len(l))

    # Load the dataset.
    examples = _load_dataset(dataset_path)
    examples = examples[:max_examples]
    print(examples[:2])
    num_examples = len(examples)
    print(f"Loaded {num_examples} examples from {dataset_path}")

    # Load tokenizer and tokenize the examples.
    t = AutoTokenizer.from_pretrained(model_name_or_path)
    tokenized_examples = [t.encode(ex) for ex in examples]

    # Launch a Process for each GPU.
    # gpus = GPUtil.getGPUs()
    # num_workers = len(gpus)
    num_workers = torch.cuda.device_count()
    processes = []
    main_queue = Queue()
    worker_queues = [Queue() for _ in range(num_workers)]
    # for i, gpu in enumerate(gpus):
    #     p = Process(target=worker, args=(model_name_or_path,
    #                                      context_len,
    #                                      stride,
    #                                      gpu.id,
    #                                      main_queue,
    #                                      worker_queues[i]))
    for i in range(num_workers):
        p = Process(target=_worker, args=(model_name_or_path,
                                         context_len,
                                         stride,
                                         i,
                                         main_queue,
                                         worker_queues[i]))
        processes.append(p)
        p.start()

    # Wait until each GPU has loaded a model.
    num_ready = 0
    while num_ready < num_workers:
        gpu_id, is_ready = main_queue.get()
        print(f"GPU {gpu_id} loaded model.")
        num_ready += 1

    # Issue requests to all worker queues, round-robin style.

    # Compute the number of examples for each shard.
    shard_counts = [(x + 1 if i < num_examples % num_shards else x)
       for i, x in enumerate([num_examples // num_shards] * num_shards)]
    shard_counts = np.asarray(shard_counts)

    # Compute the starting index (into the list of examples) for each shard.
    shard_example_indices = [0] + np.cumsum(shard_counts).tolist()
    for i, (start, end) in enumerate(zip(shard_example_indices, shard_example_indices[1:])):

        shard = tokenized_examples[start:end]

        # Logprobs in canonical order.
        worker_queues[0].put((
            flatten(shard), # tokens
            i,              # shard id
            True))          # is_canonical=True

        # Logprobs in shuffled order(s).
        for j in range(permutations_per_shard):
            w = j % num_workers
            worker_queues[w].put((
            flatten(shuffle(shard)), # tokens
            i,                       # shard id
            False))                  # is_canonical=False

    # Wait on requests.
    total_work = num_shards * (1 + permutations_per_shard)
    pbar = tqdm(total=total_work)

    canonical_logprobs = [None for _ in range(num_shards)]
    shuffled_logprobs  = [[] for _ in range(num_shards)]

    completed = 0
    while completed < total_work:

        logprob, shard_id, is_canonical = main_queue.get()

        if is_canonical:
            canonical_logprobs[shard_id] = logprob
        else:
            shuffled_logprobs[shard_id].append(logprob)

        pbar.update(1)
        completed += 1

    # Terminate workers.
    for w in range(num_workers):
        worker_queues[w].put((None, None, None))

    for p in processes:
        p.join()

    # Calculate p-value.
    canonical_logprobs = np.asarray(canonical_logprobs)
    shuffled_logprobs = np.asarray(shuffled_logprobs)

    # T-test.
    diffs = canonical_logprobs - shuffled_logprobs.mean(axis=1)
    z = np.mean(diffs) / np.std(diffs) * np.sqrt(len(diffs))
    pval = 1 - tdist.cdf(z, df=len(diffs)-1)
    print(f"{pval=}")

    # Log.
    if log_file_path is not None:
        print(f"Writing logprobs to: {log_file_path}")
        with open(f"{log_file_path}", 'w') as f:
            f.write(json.dumps({
                'pval': pval,
                'permutations_per_shard': permutations_per_shard,
                'num_shards': num_shards,
                'canonical_logprobs': canonical_logprobs.tolist(),
                'shuffled_logprobs': shuffled_logprobs.tolist(),
            }))
    # return None