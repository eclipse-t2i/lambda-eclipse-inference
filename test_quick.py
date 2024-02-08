import os
import torch
from transformers import (
    CLIPTextModelWithProjection,
    CLIPTokenizer,
)
from src.pipelines.pipeline_kandinsky_subject_prior import KandinskyPriorPipeline
from src.priors.lambda_prior_transformer import PriorTransformer
from diffusers import DiffusionPipeline

# write the argument parser
def get_parser():
    import argparse
    parser = argparse.ArgumentParser(description="Fine-tuning CLIP model on Kandinsky dataset")

    parser.add_argument("--prompt", type=str, required=True, help="Path to the checkpoint")
    parser.add_argument("--subject1_path", type=str, required=True, help="Batch size")
    parser.add_argument("--subject1_name", type=str, required=True, help="Learning rate")
    parser.add_argument("--subject2_path", type=str, default=None, help="Batch size")
    parser.add_argument("--subject2_name", type=str, default=None, help="Learning rate")
    parser.add_argument("--output_dir", type=str, default="./assets/", help="Output directory")

    args = parser.parse_args()
    return args

def main(args):
    text_encoder = CLIPTextModelWithProjection.from_pretrained(
        "laion/CLIP-ViT-bigG-14-laion2B-39B-b160k",
        projection_dim=1280,
        torch_dtype=torch.float32,
    )
    tokenizer = CLIPTokenizer.from_pretrained("laion/CLIP-ViT-bigG-14-laion2B-39B-b160k")

    prior = PriorTransformer.from_pretrained("ECLIPSE-Community/Lambda-ECLIPSE-Prior-v1.0")
    pipe_prior = KandinskyPriorPipeline.from_pretrained(
        "kandinsky-community/kandinsky-2-2-prior",
        prior=prior,
        text_encoder=text_encoder,
        tokenizer=tokenizer,
    ).to("cuda")

    pipe = DiffusionPipeline.from_pretrained(
        "kandinsky-community/kandinsky-2-2-decoder"
    ).to("cuda")

    raw_data = {
        "prompt": args.prompt,
        "subject_images": [args.subject1_path, args.subject2_path],
        "subject_keywords": [args.subject1_name, args.subject2_name]
    }
    image_emb, negative_image_emb = pipe_prior(
        raw_data=raw_data,
    ).to_tuple()
    image = pipe(
        image_embeds=image_emb,
        negative_image_embeds=negative_image_emb,
        num_inference_steps=50,
        guidance_scale=7.5,
    ).images

    image[0].save(os.path.join(args.output_dir, f'{args.prompt.replace(" ", "_")}.png'))


if __name__ == "__main__":
    args = get_parser()
    main(args)