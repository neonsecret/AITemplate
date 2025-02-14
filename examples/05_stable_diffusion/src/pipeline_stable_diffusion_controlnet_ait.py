#  Copyright (c) Meta Platforms, Inc. and affiliates.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import inspect
import os
from typing import List, Optional, Union

import torch
from aitemplate.compiler import Model
from diffusers import (
    AutoencoderKL,
    ControlNetModel,
    EulerDiscreteScheduler,
    UNet2DConditionModel,
)
from diffusers.pipelines.stable_diffusion import StableDiffusionPipelineOutput
from diffusers.utils.pil_utils import numpy_to_pil
from tqdm import tqdm
from transformers import CLIPTextConfig, CLIPTextModel, CLIPTokenizer

from .compile_lib.compile_vae_alt import map_vae
from .modeling.vae import AutoencoderKL as ait_AutoencoderKL
from .pipeline_utils import convert_ldm_unet_checkpoint, convert_ldm_vae_checkpoint, map_clip_state_dict, map_unet_state_dict


def map_controlnet_params(pt_mod):
    pt_params = dict(pt_mod.named_parameters())
    params_ait = {}
    for key, arr in pt_params.items():
        if len(arr.shape) == 4:
            arr = arr.permute((0, 2, 3, 1)).contiguous()
        elif key.endswith("ff.net.0.proj.weight"):
            w1, w2 = arr.chunk(2, dim=0)
            params_ait[key.replace(".", "_")] = w1
            params_ait[key.replace(".", "_").replace("proj", "gate")] = w2
            continue
        elif key.endswith("ff.net.0.proj.bias"):
            w1, w2 = arr.chunk(2, dim=0)
            params_ait[key.replace(".", "_")] = w1
            params_ait[key.replace(".", "_").replace("proj", "gate")] = w2
            continue
        params_ait[key.replace(".", "_")] = arr
    params_ait["controlnet_cond_embedding_conv_in_weight"] = torch.nn.functional.pad(
        params_ait["controlnet_cond_embedding_conv_in_weight"], (0, 1, 0, 0, 0, 0, 0, 0)
    )
    params_ait["arange"] = (
        torch.arange(start=0, end=320 // 2, dtype=torch.float32).to(torch.device(0)).half()
    )
    return params_ait


class StableDiffusionAITPipeline:
    def __init__(self, hf_hub_or_path, ckpt, workdir="tmp/"):
        self.device = torch.device(0)
        if ckpt is not None:
            state_dict = torch.load(ckpt, map_location="cpu")
            while "state_dict" in state_dict:
                state_dict = state_dict["state_dict"]
            clip_state_dict = {}
            unet_state_dict = {}
            vae_state_dict = {}
            for key in state_dict.keys():
                if key.startswith("cond_stage_model.transformer."):
                    new_key = key.replace("cond_stage_model.transformer.", "")
                    clip_state_dict[new_key] = state_dict[key]
                elif key.startswith("cond_stage_model.model."):
                    new_key = key.replace("cond_stage_model.model.", "")
                    clip_state_dict[new_key] = state_dict[key]
                elif key.startswith("first_stage_model."):
                    new_key = key.replace("first_stage_model.", "")
                    vae_state_dict[new_key] = state_dict[key]
                elif key.startswith("model.diffusion_model."):
                    new_key = key.replace("model.diffusion_model.", "")
                    unet_state_dict[new_key] = state_dict[key]
            # TODO: SD2.x clip support, get from diffusers convert_from_ckpt.py
            # clip_state_dict = convert_text_enc_state_dict(clip_state_dict)
            unet_state_dict = convert_ldm_unet_checkpoint(unet_state_dict)
            vae_state_dict = convert_ldm_vae_checkpoint(vae_state_dict)

        self.controlnet_ait_exe = self.init_ait_module("ControlNetModel", "./tmp")
        print("Loading PyTorch ControlNet")
        controlnet_pt = ControlNetModel.from_pretrained(
            "lllyasviel/sd-controlnet-canny", torch_dtype=torch.float16
        ).to("cuda")
        controlnet_pt.eval()
        ait_params = map_controlnet_params(controlnet_pt)
        self.controlnet_ait_exe.set_many_constants_with_tensors(ait_params)
        self.controlnet_ait_exe.fold_constants()
        self.clip_ait_exe = self.init_ait_module(
            model_name="CLIPTextModel", workdir=workdir
        )
        print("Loading PyTorch CLIP")
        if ckpt is None:
            self.clip_pt = CLIPTextModel.from_pretrained(
                hf_hub_or_path,
                subfolder="text_encoder",
                revision="fp16",
                torch_dtype=torch.float16,
            ).to(self.device)
        else:
            config = CLIPTextConfig.from_pretrained(
                hf_hub_or_path, subfolder="text_encoder"
            )
            self.clip_pt = CLIPTextModel(config)
            self.clip_pt.load_state_dict(clip_state_dict)
        clip_params_ait = map_clip_state_dict(dict(self.clip_pt.named_parameters()))
        print("Setting constants")
        self.clip_ait_exe.set_many_constants_with_tensors(clip_params_ait)
        print("Folding constants")
        self.clip_ait_exe.fold_constants()
        # cleanup
        del self.clip_pt
        del clip_params_ait

        self.unet_ait_exe = self.init_ait_module(
            model_name="ControlNetUNet2DConditionModel", workdir=workdir
        )

        print("Loading PyTorch UNet")
        if ckpt is None:
            self.unet_pt = UNet2DConditionModel.from_pretrained(
                hf_hub_or_path,
                subfolder="unet",
                revision="fp16",
                torch_dtype=torch.float16,
            ).to(self.device)
            self.unet_pt = self.unet_pt.state_dict()
        else:
            self.unet_pt = unet_state_dict
        unet_params_ait = map_unet_state_dict(self.unet_pt)
        print("Setting constants")
        self.unet_ait_exe.set_many_constants_with_tensors(unet_params_ait)
        print("Folding constants")
        self.unet_ait_exe.fold_constants()
        # cleanup
        del self.unet_pt
        del unet_params_ait

        self.vae_ait_exe = self.init_ait_module(
            model_name="AutoencoderKL", workdir=workdir
        )
        print("Loading PyTorch VAE")
        if ckpt is None:
            self.vae_pt = AutoencoderKL.from_pretrained(
                hf_hub_or_path,
                subfolder="vae",
                revision="fp16",
                torch_dtype=torch.float16,
            ).to(self.device)
        else:
            self.vae_pt = dict(vae_state_dict)
        in_channels = 3
        out_channels = 3
        down_block_types = [
            "DownEncoderBlock2D",
            "DownEncoderBlock2D",
            "DownEncoderBlock2D",
            "DownEncoderBlock2D",
        ]
        up_block_types = [
            "UpDecoderBlock2D",
            "UpDecoderBlock2D",
            "UpDecoderBlock2D",
            "UpDecoderBlock2D",
        ]
        block_out_channels = [128, 256, 512, 512]
        layers_per_block = 2
        act_fn = "silu"
        latent_channels = 4
        sample_size = 512

        ait_vae = ait_AutoencoderKL(
            1,
            64,
            64,
            in_channels=in_channels,
            out_channels=out_channels,
            down_block_types=down_block_types,
            up_block_types=up_block_types,
            block_out_channels=block_out_channels,
            layers_per_block=layers_per_block,
            act_fn=act_fn,
            latent_channels=latent_channels,
            sample_size=sample_size,
        )
        print("Mapping parameters...")
        vae_params_ait = map_vae(ait_vae, self.vae_pt)
        print("Setting constants")
        self.vae_ait_exe.set_many_constants_with_tensors(vae_params_ait)
        print("Folding constants")
        self.vae_ait_exe.fold_constants()
        # cleanup
        del self.vae_pt
        del ait_vae
        del vae_params_ait

        self.tokenizer = CLIPTokenizer.from_pretrained("openai/clip-vit-large-patch14")
        self.scheduler = EulerDiscreteScheduler.from_pretrained(
            "runwayml/stable-diffusion-v1-5", subfolder="scheduler"
        )
        self.batch = 1

    def init_ait_module(
            self,
            model_name,
            workdir,
    ):
        mod = Model(os.path.join(workdir, model_name, "test.so"))
        return mod

    def controlnet_inference(
            self, latent_model_input, timesteps, encoder_hidden_states, controlnet_cond
    ):
        exe_module = self.controlnet_ait_exe
        timesteps_pt = timesteps.expand(latent_model_input.shape[0])
        inputs = {
            "input0": latent_model_input.permute((0, 2, 3, 1))
            .contiguous()
            .to(self.device)
            .half(),
            "input1": timesteps_pt.to(self.device).half(),
            "input2": encoder_hidden_states.to(self.device).half(),
            "input3": controlnet_cond.permute((0, 2, 3, 1)).contiguous().to(self.device).half(),
        }
        ys = []
        num_outputs = len(exe_module.get_output_name_to_index_map())
        for i in range(num_outputs):
            shape = exe_module.get_output_maximum_shape(i)
            ys.append(torch.empty(shape).to(self.device).half())
        exe_module.run_with_tensors(inputs, ys, graph_mode=False)
        down_block_residuals = (y for y in ys[:-1])
        mid_block_residuals = ys[-1]
        return down_block_residuals, mid_block_residuals

    def unet_inference(
            self,
            latent_model_input,
            timesteps,
            encoder_hidden_states,
            height,
            width,
            down_block_residuals,
            mid_block_residual,
    ):
        exe_module = self.unet_ait_exe
        timesteps_pt = timesteps.expand(self.batch * 2)
        inputs = {
            "input0": latent_model_input.permute((0, 2, 3, 1))
            .contiguous()
            .to(self.device)
            .half(),
            "input1": timesteps_pt.to(self.device).half(),
            "input2": encoder_hidden_states.to(self.device).half(),
        }
        for i, y in enumerate(down_block_residuals):
            inputs[f"down_block_residual_{i}"] = y
        inputs["mid_block_residual"] = mid_block_residual
        ys = []
        num_outputs = len(exe_module.get_output_name_to_index_map())
        for i in range(num_outputs):
            shape = exe_module.get_output_maximum_shape(i)
            shape[0] = self.batch * 2
            shape[1] = height // 8
            shape[2] = width // 8
            ys.append(torch.empty(shape).to(self.device).half())
        exe_module.run_with_tensors(inputs, ys, graph_mode=False)
        noise_pred = ys[0].permute((0, 3, 1, 2)).float()
        return noise_pred

    def clip_inference(self, input_ids, seqlen=77):
        exe_module = self.clip_ait_exe
        bs = input_ids.shape[0]
        position_ids = torch.arange(seqlen).expand((bs, -1)).to(self.device)
        inputs = {
            "input0": input_ids,
            "input1": position_ids,
        }
        ys = []
        num_outputs = len(exe_module.get_output_name_to_index_map())
        for i in range(num_outputs):
            shape = exe_module.get_output_maximum_shape(i)
            shape[0] = self.batch
            ys.append(torch.empty(shape).to(self.device).half())
        exe_module.run_with_tensors(inputs, ys, graph_mode=False)
        return ys[0].float()

    def vae_inference(self, vae_input, height, width):
        exe_module = self.vae_ait_exe
        inputs = [torch.permute(vae_input, (0, 2, 3, 1)).contiguous().to(self.device).half()]
        ys = []
        num_outputs = len(exe_module.get_output_name_to_index_map())
        for i in range(num_outputs):
            shape = exe_module.get_output_maximum_shape(i)
            shape[0] = self.batch * 2
            shape[1] = height
            shape[2] = width
            ys.append(torch.empty(shape).to(self.device).half())
        exe_module.run_with_tensors(inputs, ys, graph_mode=False)
        vae_out = ys[0].permute((0, 3, 1, 2)).float()
        return vae_out

    @torch.no_grad()
    def __call__(
            self,
            prompt: Union[str, List[str]],
            control_cond: torch.FloatTensor,
            height: Optional[int] = 512,
            width: Optional[int] = 512,
            num_inference_steps: Optional[int] = 50,
            guidance_scale: Optional[float] = 7.5,
            negative_prompt: Optional[Union[str, List[str]]] = None,
            eta: Optional[float] = 0.0,
            generator: Optional[torch.Generator] = None,
            latents: Optional[torch.FloatTensor] = None,
            output_type: Optional[str] = "pil",
            return_dict: bool = True,
    ):
        r"""
        Function invoked when calling the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`):
                The prompt or prompts to guide the image generation.
            height (`int`, *optional*, defaults to 512):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to 512):
                The width in pixels of the generated image.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            guidance_scale (`float`, *optional*, defaults to 7.5):
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined  as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt`,
                usually at the expense of lower image quality.
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the image generation. Ignored when not using guidance (i.e., ignored
                if `guidance_scale` is less than `1`).
            eta (`float`, *optional*, defaults to 0.0):
                Corresponds to parameter eta (η) in the DDIM paper: https://arxiv.org/abs/2010.02502. Only applies to
                [`schedulers.DDIMScheduler`], will be ignored for others.
            generator (`torch.Generator`, *optional*):
                A [torch generator](https://pytorch.org/docs/stable/generated/torch.Generator.html) to make generation
                deterministic.
            latents (`torch.FloatTensor`, *optional*):
                Pre-generated noisy latents, sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor will ge generated by sampling using the supplied random `generator`.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generate image. Choose between
                [PIL](https://pillow.readthedocs.io/en/stable/): `PIL.Image.Image` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] instead of a
                plain tuple.

        Returns:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] or `tuple`:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] if `return_dict` is True, otherwise a `tuple.
            When returning a tuple, the first element is a list with the generated images, and the second element is a
            list of `bool`s denoting whether the corresponding generated image likely represents "not-safe-for-work"
            (nsfw) content, according to the `safety_checker`.
        """

        if isinstance(prompt, str):
            batch_size = 1
        elif isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            raise ValueError(
                f"`prompt` has to be of type `str` or `list` but is {type(prompt)}"
            )

        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(
                f"`height` and `width` have to be divisible by 8 but are {height} and {width}."
            )

        self.batch = batch_size

        # get prompt text embeddings
        text_input = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="pt",
        )
        text_embeddings = self.clip_inference(text_input.input_ids.to(self.device))
        # pytorch equivalent
        # text_embeddings = self.clip_pt(text_input.input_ids.to(self.device)).last_hidden_state

        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0
        # get unconditional embeddings for classifier free guidance
        if do_classifier_free_guidance:
            uncond_tokens: List[str]
            max_length = text_input.input_ids.shape[-1]
            if negative_prompt is None:
                uncond_tokens = [""] * batch_size
            elif type(prompt) is not type(negative_prompt):
                raise TypeError(
                    f"`negative_prompt` should be the same type to `prompt`, but got {type(negative_prompt)} !="
                    f" {type(prompt)}."
                )
            elif isinstance(negative_prompt, str):
                uncond_tokens = [negative_prompt]
            elif batch_size != len(negative_prompt):
                raise ValueError(
                    f"`negative_prompt`: {negative_prompt} has batch size {len(negative_prompt)}, but `prompt`:"
                    f" {prompt} has batch size {batch_size}. Please make sure that passed `negative_prompt` matches"
                    " the batch size of `prompt`."
                )
            else:
                uncond_tokens = negative_prompt
            uncond_input = self.tokenizer(
                uncond_tokens,
                padding="max_length",
                max_length=max_length,
                truncation=True,
                return_tensors="pt",
            )
            uncond_embeddings = self.clip_inference(
                uncond_input.input_ids.to(self.device)
            )

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            text_embeddings = torch.cat([uncond_embeddings, text_embeddings])

        # get the initial random noise unless the user supplied it

        # Unlike in other pipelines, latents need to be generated in the target device
        # for 1-to-1 results reproducibility with the CompVis implementation.
        # However this currently doesn't work in `mps`.
        latents_device = self.device
        latents_shape = (batch_size, 4, height // 8, width // 8)
        if latents is None:
            latents = torch.randn(
                latents_shape,
                generator=generator,
                device=latents_device,
            )
        else:
            if latents.shape != latents_shape:
                raise ValueError(
                    f"Unexpected latents shape, got {latents.shape}, expected {latents_shape}"
                )
        latents = latents.to(self.device)

        # set timesteps
        accepts_offset = "offset" in set(
            inspect.signature(self.scheduler.set_timesteps).parameters.keys()
        )
        extra_set_kwargs = {}
        if accepts_offset:
            extra_set_kwargs["offset"] = 1

        self.scheduler.set_timesteps(num_inference_steps, **extra_set_kwargs)

        latents = latents * self.scheduler.init_noise_sigma

        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]
        accepts_eta = "eta" in set(
            inspect.signature(self.scheduler.step).parameters.keys()
        )
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta
            # check if the scheduler accepts generator
        accepts_generator = "generator" in set(
            inspect.signature(self.scheduler.step).parameters.keys()
        )
        if accepts_generator:
            extra_step_kwargs["generator"] = generator

        for t in tqdm(self.scheduler.timesteps):
            # expand the latents if we are doing classifier free guidance
            latent_model_input = (
                torch.cat([latents] * 2) if do_classifier_free_guidance else latents
            )
            latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)
            down_block_residuals, mid_block_residual = self.controlnet_inference(
                latent_model_input, t, text_embeddings, control_cond
            )
            # predict the noise residual
            noise_pred = self.unet_inference(
                latent_model_input,
                t,
                encoder_hidden_states=text_embeddings,
                height=height,
                width=width,
                down_block_residuals=down_block_residuals,
                mid_block_residual=mid_block_residual,
            )

            # perform guidance
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (
                        noise_pred_text - noise_pred_uncond
                )

            latents = self.scheduler.step(
                noise_pred, t, latents, **extra_step_kwargs
            ).prev_sample

        # scale and decode the image latents with vae
        latents = 1 / 0.18215 * latents
        image = self.vae_inference(latents, height, width)
        # pytorch equivalent
        # image = self.vae_pt.decode(latents).sample

        image = (image / 2 + 0.5).clamp(0, 1)
        image = image.cpu().permute(0, 2, 3, 1).numpy()

        has_nsfw_concept = None

        if output_type == "pil":
            image = numpy_to_pil(image)

        if not return_dict:
            return image, has_nsfw_concept

        return StableDiffusionPipelineOutput(
            images=image, nsfw_content_detected=has_nsfw_concept
        )
