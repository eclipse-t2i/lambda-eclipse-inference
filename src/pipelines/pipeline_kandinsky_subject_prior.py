from dataclasses import dataclass
from typing import List, Optional, Union

import numpy as np
from PIL import Image
import PIL
import torch
from transformers import (
    CLIPImageProcessor,
    CLIPTextModelWithProjection,
    CLIPTokenizer,
    CLIPVisionModelWithProjection,
)

from diffusers.models import PriorTransformer
from diffusers.schedulers import UnCLIPScheduler
from diffusers.utils import (
    BaseOutput,
    is_accelerate_available,
    is_accelerate_version,
    logging,
    # randn_tensor,
    replace_example_docstring,
)
from diffusers.utils.torch_utils import randn_tensor
from diffusers.pipelines.pipeline_utils import DiffusionPipeline


logger = logging.get_logger(__name__)  # pylint: disable=invalid-name

EXAMPLE_DOC_STRING = """
    Examples:
        ```py
        >>> from diffusers import KandinskyPipeline, KandinskyPriorPipeline
        >>> import torch

        >>> pipe_prior = KandinskyPriorPipeline.from_pretrained("kandinsky-community/kandinsky-2-1-prior")
        >>> pipe_prior.to("cuda")

        >>> prompt = "red cat, 4k photo"
        >>> out = pipe_prior(prompt)
        >>> image_emb = out.image_embeds
        >>> negative_image_emb = out.negative_image_embeds

        >>> pipe = KandinskyPipeline.from_pretrained("kandinsky-community/kandinsky-2-1")
        >>> pipe.to("cuda")

        >>> image = pipe(
        ...     prompt,
        ...     image_embeds=image_emb,
        ...     negative_image_embeds=negative_image_emb,
        ...     height=768,
        ...     width=768,
        ...     num_inference_steps=100,
        ... ).images

        >>> image[0].save("cat.png")
        ```
"""

EXAMPLE_INTERPOLATE_DOC_STRING = """
    Examples:
        ```py
        >>> from diffusers import KandinskyPriorPipeline, KandinskyPipeline
        >>> from diffusers.utils import load_image
        >>> import PIL

        >>> import torch
        >>> from torchvision import transforms

        >>> pipe_prior = KandinskyPriorPipeline.from_pretrained(
        ...     "kandinsky-community/kandinsky-2-1-prior", torch_dtype=torch.float16
        ... )
        >>> pipe_prior.to("cuda")

        >>> img1 = load_image(
        ...     "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main"
        ...     "/kandinsky/cat.png"
        ... )

        >>> img2 = load_image(
        ...     "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main"
        ...     "/kandinsky/starry_night.jpeg"
        ... )

        >>> images_texts = ["a cat", img1, img2]
        >>> weights = [0.3, 0.3, 0.4]
        >>> image_emb, zero_image_emb = pipe_prior.interpolate(images_texts, weights)

        >>> pipe = KandinskyPipeline.from_pretrained("kandinsky-community/kandinsky-2-1", torch_dtype=torch.float16)
        >>> pipe.to("cuda")

        >>> image = pipe(
        ...     "",
        ...     image_embeds=image_emb,
        ...     negative_image_embeds=zero_image_emb,
        ...     height=768,
        ...     width=768,
        ...     num_inference_steps=150,
        ... ).images[0]

        >>> image.save("starry_cat.png")
        ```
"""


@dataclass
class KandinskyPriorPipelineOutput(BaseOutput):
    """
    Output class for KandinskyPriorPipeline.

    Args:
        image_embeds (`torch.FloatTensor`)
            clip image embeddings for text prompt
        negative_image_embeds (`List[PIL.Image.Image]` or `np.ndarray`)
            clip image embeddings for unconditional tokens
    """

    image_embeds: Union[torch.FloatTensor, np.ndarray]
    negative_image_embeds: Union[torch.FloatTensor, np.ndarray]


class KandinskyPriorPipeline(DiffusionPipeline):
    """
    Pipeline for generating image prior for Kandinsky

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods the
    library implements for all the pipelines (such as downloading or saving, running on a particular device, etc.)

    Args:
        prior ([`PriorTransformer`]):
            The canonincal unCLIP prior to approximate the image embedding from the text embedding.
        image_encoder ([`CLIPVisionModelWithProjection`]):
            Frozen image-encoder.
        text_encoder ([`CLIPTextModelWithProjection`]):
            Frozen text-encoder.
        tokenizer (`CLIPTokenizer`):
            Tokenizer of class
            [CLIPTokenizer](https://huggingface.co/docs/transformers/v4.21.0/en/model_doc/clip#transformers.CLIPTokenizer).
        scheduler ([`UnCLIPScheduler`]):
            A scheduler to be used in combination with `prior` to generate image embedding.
    """

    _exclude_from_cpu_offload = ["prior"]

    def __init__(
        self,
        prior: PriorTransformer,
        image_encoder: CLIPVisionModelWithProjection,
        text_encoder: CLIPTextModelWithProjection,
        tokenizer: CLIPTokenizer,
        scheduler: UnCLIPScheduler,
        image_processor: CLIPImageProcessor,
    ):
        super().__init__()

        self.register_modules(
            prior=prior,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            scheduler=scheduler,
            image_encoder=image_encoder,
            image_processor=image_processor,
        )

    @torch.no_grad()
    @replace_example_docstring(EXAMPLE_INTERPOLATE_DOC_STRING)
    def interpolate(
        self,
        images_and_prompts: List[Union[str, PIL.Image.Image, torch.FloatTensor]],
        weights: List[float],
        num_images_per_prompt: int = 1,
        num_inference_steps: int = 25,
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
        latents: Optional[torch.FloatTensor] = None,
        negative_prior_prompt: Optional[str] = None,
        negative_prompt: str = "",
        guidance_scale: float = 4.0,
        device=None,
    ):
        """
        Function invoked when using the prior pipeline for interpolation.

        Args:
            images_and_prompts (`List[Union[str, PIL.Image.Image, torch.FloatTensor]]`):
                list of prompts and images to guide the image generation.
            weights: (`List[float]`):
                list of weights for each condition in `images_and_prompts`
            num_images_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            num_inference_steps (`int`, *optional*, defaults to 25):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            generator (`torch.Generator` or `List[torch.Generator]`, *optional*):
                One or a list of [torch generator(s)](https://pytorch.org/docs/stable/generated/torch.Generator.html)
                to make generation deterministic.
            latents (`torch.FloatTensor`, *optional*):
                Pre-generated noisy latents, sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor will ge generated by sampling using the supplied random `generator`.
            negative_prior_prompt (`str`, *optional*):
                The prompt not to guide the prior diffusion process. Ignored when not using guidance (i.e., ignored if
                `guidance_scale` is less than `1`).
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt not to guide the image generation. Ignored when not using guidance (i.e., ignored if
                `guidance_scale` is less than `1`).
            guidance_scale (`float`, *optional*, defaults to 4.0):
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt`,
                usually at the expense of lower image quality.

        Examples:

        Returns:
            [`KandinskyPriorPipelineOutput`] or `tuple`
        """

        device = device or self.device

        if len(images_and_prompts) != len(weights):
            raise ValueError(
                f"`images_and_prompts` contains {len(images_and_prompts)} items and `weights` contains {len(weights)} items - they should be lists of same length"
            )

        image_embeddings = []
        for cond, weight in zip(images_and_prompts, weights):
            if isinstance(cond, str):
                image_emb = self(
                    cond,
                    num_inference_steps=num_inference_steps,
                    num_images_per_prompt=num_images_per_prompt,
                    generator=generator,
                    latents=latents,
                    negative_prompt=negative_prior_prompt,
                    guidance_scale=guidance_scale,
                ).image_embeds

            elif isinstance(cond, (PIL.Image.Image, torch.Tensor)):
                if isinstance(cond, PIL.Image.Image):
                    cond = (
                        self.image_processor(cond, return_tensors="pt")
                        .pixel_values[0]
                        .unsqueeze(0)
                        .to(dtype=self.image_encoder.dtype, device=device)
                    )

                image_emb = self.image_encoder(cond)["image_embeds"]

            else:
                raise ValueError(
                    f"`images_and_prompts` can only contains elements to be of type `str`, `PIL.Image.Image` or `torch.Tensor`  but is {type(cond)}"
                )

            image_embeddings.append(image_emb * weight)

        image_emb = torch.cat(image_embeddings).sum(dim=0, keepdim=True)

        out_zero = self(
            negative_prompt,
            num_inference_steps=num_inference_steps,
            num_images_per_prompt=num_images_per_prompt,
            generator=generator,
            latents=latents,
            negative_prompt=negative_prior_prompt,
            guidance_scale=guidance_scale,
        )
        zero_image_emb = (
            out_zero.negative_image_embeds
            if negative_prompt == ""
            else out_zero.image_embeds
        )

        return KandinskyPriorPipelineOutput(
            image_embeds=image_emb, negative_image_embeds=zero_image_emb
        )

    # Copied from diffusers.pipelines.unclip.pipeline_unclip.UnCLIPPipeline.prepare_latents
    def prepare_latents(self, shape, dtype, device, generator, latents, scheduler):
        if latents is None:
            latents = randn_tensor(
                shape, generator=generator, device=device, dtype=dtype
            )
        else:
            if latents.shape != shape:
                raise ValueError(
                    f"Unexpected latents shape, got {latents.shape}, expected {shape}"
                )
            latents = latents.to(device)

        latents = latents * scheduler.init_noise_sigma
        return latents

    def get_zero_embed(self, batch_size=1, device=None):
        device = device or self.device
        zero_img = torch.zeros(
            1,
            3,
            self.image_encoder.config.image_size,
            self.image_encoder.config.image_size,
        ).to(device=device, dtype=self.image_encoder.dtype)
        zero_image_emb = self.image_encoder(zero_img)["image_embeds"]
        zero_image_emb = zero_image_emb.repeat(batch_size, 1)
        return zero_image_emb

    def _encode_prompt(
        self,
        prompt,
        device,
        num_images_per_prompt,
        do_classifier_free_guidance,
        negative_prompt=None,
    ):
        batch_size = len(prompt) if isinstance(prompt, list) else 1
        # get prompt text embeddings
        text_inputs = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="pt",
        )
        text_input_ids = text_inputs.input_ids
        text_mask = text_inputs.attention_mask.bool().to(device)

        untruncated_ids = self.tokenizer(
            prompt, padding="longest", return_tensors="pt"
        ).input_ids

        if untruncated_ids.shape[-1] >= text_input_ids.shape[-1] and not torch.equal(
            text_input_ids, untruncated_ids
        ):
            removed_text = self.tokenizer.batch_decode(
                untruncated_ids[:, self.tokenizer.model_max_length - 1 : -1]
            )
            logger.warning(
                "The following part of your input was truncated because CLIP can only handle sequences up to"
                f" {self.tokenizer.model_max_length} tokens: {removed_text}"
            )
            text_input_ids = text_input_ids[:, : self.tokenizer.model_max_length]

        text_encoder_output = self.text_encoder(text_input_ids.to(device))

        prompt_embeds = text_encoder_output.text_embeds
        text_encoder_hidden_states = text_encoder_output.last_hidden_state

        prompt_embeds = prompt_embeds.repeat_interleave(num_images_per_prompt, dim=0)
        text_encoder_hidden_states = text_encoder_hidden_states.repeat_interleave(
            num_images_per_prompt, dim=0
        )
        text_mask = text_mask.repeat_interleave(num_images_per_prompt, dim=0)

        if do_classifier_free_guidance:
            uncond_tokens: List[str]
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
                max_length=self.tokenizer.model_max_length,
                truncation=True,
                return_tensors="pt",
            )
            uncond_text_mask = uncond_input.attention_mask.bool().to(device)
            negative_prompt_embeds_text_encoder_output = self.text_encoder(
                uncond_input.input_ids.to(device)
            )

            negative_prompt_embeds = (
                negative_prompt_embeds_text_encoder_output.text_embeds
            )
            uncond_text_encoder_hidden_states = (
                negative_prompt_embeds_text_encoder_output.last_hidden_state
            )

            # duplicate unconditional embeddings for each generation per prompt, using mps friendly method

            seq_len = negative_prompt_embeds.shape[1]
            negative_prompt_embeds = negative_prompt_embeds.repeat(
                1, num_images_per_prompt
            )
            negative_prompt_embeds = negative_prompt_embeds.view(
                batch_size * num_images_per_prompt, seq_len
            )

            seq_len = uncond_text_encoder_hidden_states.shape[1]
            uncond_text_encoder_hidden_states = (
                uncond_text_encoder_hidden_states.repeat(1, num_images_per_prompt, 1)
            )
            uncond_text_encoder_hidden_states = uncond_text_encoder_hidden_states.view(
                batch_size * num_images_per_prompt, seq_len, -1
            )
            uncond_text_mask = uncond_text_mask.repeat_interleave(
                num_images_per_prompt, dim=0
            )

            # done duplicates

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            prompt_embeds = torch.cat([negative_prompt_embeds, prompt_embeds])
            text_encoder_hidden_states = torch.cat(
                [uncond_text_encoder_hidden_states, text_encoder_hidden_states]
            )

            text_mask = torch.cat([uncond_text_mask, text_mask])

        return prompt_embeds, text_encoder_hidden_states, text_mask

    def enable_model_cpu_offload(self, gpu_id=0):
        r"""
        Offloads all models to CPU using accelerate, reducing memory usage with a low impact on performance. Compared
        to `enable_sequential_cpu_offload`, this method moves one whole model at a time to the GPU when its `forward`
        method is called, and the model remains in GPU until the next model runs. Memory savings are lower than with
        `enable_sequential_cpu_offload`, but performance is much better due to the iterative execution of the `unet`.
        """
        if is_accelerate_available() and is_accelerate_version(">=", "0.17.0.dev0"):
            from accelerate import cpu_offload_with_hook
        else:
            raise ImportError(
                "`enable_model_cpu_offload` requires `accelerate v0.17.0` or higher."
            )

        device = torch.device(f"cuda:{gpu_id}")

        if self.device.type != "cpu":
            self.to("cpu", silence_dtype_warnings=True)
            torch.cuda.empty_cache()  # otherwise we don't see the memory savings (but they probably exist)

        hook = None
        for cpu_offloaded_model in [self.text_encoder, self.prior]:
            _, hook = cpu_offload_with_hook(
                cpu_offloaded_model, device, prev_module_hook=hook
            )

        # We'll offload the last model manually.
        self.prior_hook = hook

        _, hook = cpu_offload_with_hook(
            self.image_encoder, device, prev_module_hook=self.prior_hook
        )

        self.final_offload_hook = hook

    @torch.no_grad()
    def get_text_feats(self, raw_data):
        prompt = raw_data["prompt"]
        txt = self.tokenizer(
            prompt,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )
        txt_items = {k: v.to(self._execution_device) for k, v in txt.items()}
        txt_feats = self.text_encoder(**txt_items)
        last_hidden_states = txt_feats.last_hidden_state[0].detach().cpu().numpy()
        prompt_embeds = txt_feats.text_embeds.detach().cpu()
        attention_mask = txt_items["attention_mask"]

        for sub_img, sub_name in zip(raw_data["subject_images"], raw_data["subject_keywords"]):
            if isinstance(sub_img, str):
                sub_img = Image.open(sub_img)
            mask_img = self.image_processor(sub_img, return_tensors="pt").to(self._execution_device)
            vision_feats = self.image_encoder(**mask_img).image_embeds
            entity_tokens = self.tokenizer(sub_name)["input_ids"][1:-1]

            found = True
            for tid in entity_tokens:
                indices = np.where(txt_items["input_ids"][0].cpu().numpy() == tid)[0]
                if len(indices)==0:
                    found = False
                last_hidden_states[indices] = vision_feats[0].cpu().numpy()

            if not found:
                print(f"Couldn't find keyword '{sub_name}' in the prompt.")
        
        text_feats = {
            "prompt_embeds": prompt_embeds,
            "text_encoder_hidden_states": torch.tensor(last_hidden_states).unsqueeze(0),
            "text_mask": attention_mask,
        }

        return text_feats

    @torch.no_grad()
    @replace_example_docstring(EXAMPLE_DOC_STRING)
    def __call__(
        self,
        text_feats: dict = None,
        raw_data: dict = None,
        num_images_per_prompt: int = 1,
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
        latents: Optional[torch.FloatTensor] = None,
        output_type: Optional[str] = "pt",
        return_dict: bool = True,
        control_embedding: torch.FloatTensor = None,
    ):
        """
        Function invoked when calling the pipeline for generation.

        Args:
            text_feats (`dict`, *optional*, defaults to None):
                "prompt_embeds", "text_encoder_hidden_states", "text_mask"
            raw_data (`dict`, *optional*, defaults to None):
                "prompt": str,
                "subject_images": List of str or PIL
                "subject_keywords": List of str
            num_images_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            num_inference_steps (`int`, *optional*, defaults to 25):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            generator (`torch.Generator` or `List[torch.Generator]`, *optional*):
                One or a list of [torch generator(s)](https://pytorch.org/docs/stable/generated/torch.Generator.html)
                to make generation deterministic.
            latents (`torch.FloatTensor`, *optional*):
                Pre-generated noisy latents, sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor will ge generated by sampling using the supplied random `generator`.
            output_type (`str`, *optional*, defaults to `"pt"`):
                The output format of the generate image. Choose between: `"np"` (`np.array`) or `"pt"`
                (`torch.Tensor`).
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.ImagePipelineOutput`] instead of a plain tuple.

        Examples:

        Returns:
            [`KandinskyPriorPipelineOutput`] or `tuple`
        """
        assert text_feats or raw_data, "please provide wither raw_data or pre-processed text-feats"
        assert num_images_per_prompt==1

        if text_feats is None:
            text_feats = self.get_text_feats(raw_data)

        device = self._execution_device
        for k,v in text_feats.items():
            text_feats[k] = v.to(device)

        if control_embedding is None:
            control_embedding = self.get_zero_embed(1, device=device)

        batch_size = text_feats["prompt_embeds"].shape[0]
        assert batch_size == 1

        batch_size = batch_size * num_images_per_prompt

        prompt_embeds = text_feats["prompt_embeds"]
        text_encoder_hidden_states = text_feats["text_encoder_hidden_states"]
        text_mask = text_feats["text_mask"]

        hidden_states = randn_tensor(
            (batch_size, prompt_embeds.shape[-1]),
            device=prompt_embeds.device,
            dtype=prompt_embeds.dtype,
            generator=generator,
        )

        latents = self.prior(
            hidden_states,
            proj_embedding=prompt_embeds,
            encoder_hidden_states=text_encoder_hidden_states,
            attention_mask=text_mask,
            control_embedding=control_embedding,
        ).predicted_image_embedding

        image_embeddings = latents

        # if negative prompt has been defined, we retrieve split the image embedding into two
        negative_prompt = None
        if negative_prompt is None:
            zero_embeds = self.get_zero_embed(latents.shape[0], device=latents.device)

            if (
                hasattr(self, "final_offload_hook")
                and self.final_offload_hook is not None
            ):
                self.final_offload_hook.offload()
        else:
            image_embeddings, zero_embeds = image_embeddings.chunk(2)

            if (
                hasattr(self, "final_offload_hook")
                and self.final_offload_hook is not None
            ):
                self.prior_hook.offload()

        if output_type not in ["pt", "np"]:
            raise ValueError(
                f"Only the output types `pt` and `np` are supported not output_type={output_type}"
            )

        if output_type == "np":
            image_embeddings = image_embeddings.cpu().numpy()
            zero_embeds = zero_embeds.cpu().numpy()

        if not return_dict:
            return (image_embeddings, zero_embeds)

        return KandinskyPriorPipelineOutput(
            image_embeds=image_embeddings, negative_image_embeds=zero_embeds
        )
