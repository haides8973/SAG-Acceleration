"""
Generate a large batch of image samples from a model and save them as a large
numpy array. This can be used to produce samples for FID evaluation.
"""

import argparse
import os

import numpy as np
import torch as th
import torch.distributed as dist
import yaml

from diffusion_skip import dist_util, logger
from diffusion_skip.script_util import (
    NUM_CLASSES,
    model_and_diffusion_defaults,
    create_model_and_diffusion,
    add_dict_to_argparser,
    args_to_dict,
    sag_defaults,
)
import datetime

def get_datetime():
    UTC = datetime.timezone(datetime.timedelta(hours=0))
    date = datetime.datetime.now(UTC).strftime("%Y_%m_%d-%I%M%S_%p")
    return date

def main():
    args = create_argparser().parse_args()
    save_name = f"{get_datetime()}"
    dist_util.setup_dist()
    logger.configure(dir=f'RESULTS/{save_name}')

    with open(os.path.join(logger.get_dir(), 'config.yaml'), 'w') as f:
        yaml.dump(args.__dict__, f)

    logger.log("creating model and diffusion...")
    model, diffusion = create_model_and_diffusion(
        sel_attn_depth=args.sel_attn_depth,
        sel_attn_block=args.sel_attn_block,
        **args_to_dict(args, model_and_diffusion_defaults().keys())
    )
    model.load_state_dict(
        dist_util.load_state_dict(args.model_path, map_location="cpu")
    )
    model.to(dist_util.dev())
    if args.use_fp16:
        model.convert_to_fp16()
    model.eval()

    logger.log("sampling...")
    all_images = []
    all_labels = []
    guidance_kwargs = {}
    guidance_kwargs["guide_start"] = args.guide_start
    guidance_kwargs["guide_scale"] = args.guide_scale
    guidance_kwargs["blur_sigma"] = args.blur_sigma

    start = datetime.datetime.now()

    while len(all_images) * args.batch_size < args.num_samples:
        model_kwargs = {}
        if args.class_cond:
            classes = th.randint(
                low=0, high=NUM_CLASSES, size=(args.batch_size,), device=dist_util.dev()
            )
            model_kwargs["y"] = classes
        sample_fn = (
            diffusion.p_sample_loop if not args.use_ddim else diffusion.ddim_sample_loop
        )
        sample = sample_fn(
            model,
            (args.batch_size, 3, args.image_size, args.image_size),
            clip_denoised=args.clip_denoised,
            model_kwargs=model_kwargs,
            guidance_kwargs=guidance_kwargs
        )
        sample = ((sample + 1) * 127.5).clamp(0, 255).to(th.uint8)
        sample = sample.permute(0, 2, 3, 1)
        sample = sample.contiguous()

        gathered_samples = [th.zeros_like(sample) for _ in range(dist.get_world_size())]
        dist.all_gather(gathered_samples, sample)  # gather not supported with NCCL
        all_images.extend([sample.cpu().numpy() for sample in gathered_samples])
        if args.class_cond:
            gathered_labels = [
                th.zeros_like(classes) for _ in range(dist.get_world_size())
            ]
            dist.all_gather(gathered_labels, classes)
            all_labels.extend([labels.cpu().numpy() for labels in gathered_labels])
        logger.log(f"created {len(all_images) * args.batch_size} samples")

    arr = np.concatenate(all_images, axis=0)
    arr = arr[: args.num_samples]
    if args.class_cond:
        label_arr = np.concatenate(all_labels, axis=0)
        label_arr = label_arr[: args.num_samples]
    if dist.get_rank() == 0:
        shape_str = "x".join([str(x) for x in arr.shape])
        out_path = os.path.join(logger.get_dir(), f"samples_{shape_str}.npz")
        logger.log(f"saving to {out_path}")
        if args.class_cond:
            np.savez(out_path, arr, label_arr)
        else:
            np.savez(out_path, arr)

    dist.barrier()

    end = datetime.datetime.now()
    total_seconds = (end-start).total_seconds()
    logger.log(f"sampling completed at {datetime.datetime.now()}")
    logger.log(f"{total_seconds} seconds to sample {args.num_samples} images.")


def create_argparser():
    defaults = dict(
        clip_denoised=True,
        num_samples=10000,
        batch_size=16,
        use_ddim=False,
        model_path="",
    )

    def update_(default : dict, d : dict):
        for key in d:
            if key not in default:
                default[key] = d[key]

    update_(defaults, model_and_diffusion_defaults())
    update_(defaults, sag_defaults())
    parser = argparse.ArgumentParser()
    add_dict_to_argparser(parser, defaults)
    return parser


if __name__ == "__main__":
    main()
