"""WorldBox prompt-batch script for AUTOMATIC1111-compatible WebUIs.

Renders several DIFFERENT prompts as one true GPU batch. The plain
/sdapi/v1/txt2img API only accepts a single prompt string (batch_size just
replicates it), but the WebUI's processing internals accept a list with one
prompt per image -- this script exposes that: WorldBox invokes it through the
API (script_name/script_args) with a JSON array of prompts, and every image
in the array renders in a single denoising batch.

Install: copy this file into the WebUI's scripts/ folder and restart the
WebUI (the bundled image_server launcher does this automatically). WorldBox
detects it via /sdapi/v1/scripts; without it, multi-image generations simply
fall back to one request per image.

All prompts in one call share the checkpoint, render settings, and LoRA set
(the WebUI applies extra networks batch-wide) -- WorldBox groups its
requests accordingly. Each image still gets its own seed.
"""
import json

import gradio as gr

import modules.scripts as scripts
from modules.processing import process_images


class WorldBoxPromptBatch(scripts.Script):
    def title(self):
        # Matched case-insensitively by WorldBox's script_name/probe.
        return "WorldBox Prompt Batch"

    def show(self, is_img2img):
        return not is_img2img

    def ui(self, is_img2img):
        return [gr.Textbox(label="Prompts (JSON array of strings)",
                           value="[]")]

    def run(self, p, prompts_json):
        prompts = json.loads(prompts_json or "[]")
        if (not isinstance(prompts, list) or not prompts
                or not all(isinstance(x, str) for x in prompts)):
            raise ValueError(
                "WorldBox Prompt Batch: script_args[0] must be a non-empty "
                "JSON array of prompt strings")
        p.prompt = list(prompts)   # a list reaches all_prompts verbatim
        p.batch_size = len(prompts)
        p.n_iter = 1
        p.do_not_save_grid = True  # the caller wants images, never a grid
        return process_images(p)
