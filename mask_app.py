from pathlib import Path

import torch

# use bfloat16 for the entire notebook
torch.autocast(device_type="cuda", dtype=torch.bfloat16).__enter__()

if torch.cuda.is_available() and torch.cuda.get_device_properties(0).major >= 8:
    # turn on tfloat32 for Ampere GPUs (https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices)
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True

# torch.multiprocessing.set_start_method("spawn")

import colorsys
import datetime
import os
import subprocess

import cv2
import gradio as gr
import imageio.v2 as iio
import numpy as np
from loguru import logger as guru

from sam2.build_sam import build_sam2_video_predictor


class PromptGUI(object):
    def __init__(self, checkpoint_dir, model_cfg):
        self.checkpoint_dir = checkpoint_dir
        self.model_cfg = model_cfg
        self.sam_model = None
        self.tracker = None

        self.selected_points = []
        self.selected_labels = []
        self.cur_label_val = 1.0

        self.frame_index = 0
        self.image = None
        self.cur_mask_idx = 0
        # can store multiple object masks
        # saves the masks and logits for each mask index
        self.cur_masks = {}
        self.cur_logits = {}
        self.index_masks_all = []
        self.color_masks_all = []

        self.img_dir = ""
        self.img_paths = []
        self.init_sam_model()

    def init_sam_model(self):
        if self.sam_model is None:
            self.sam_model = build_sam2_video_predictor(
                self.model_cfg, self.checkpoint_dir
            )
            guru.info(f"loaded model checkpoint {self.checkpoint_dir}")

    def clear_points(self) -> tuple[None, None, str]:
        self.selected_points.clear()
        self.selected_labels.clear()
        message = "Cleared points, select new points to update mask"
        return None, message

    def add_new_mask(self):
        self.cur_mask_idx += 1
        self.clear_points()
        message = f"Creating new mask with index {self.cur_mask_idx}"
        return None, message

    def make_index_mask(self, masks):
        assert len(masks) > 0
        idcs = list(masks.keys())
        idx_mask = masks[idcs[0]].astype("uint8")
        for i in idcs:
            mask = masks[i]
            idx_mask[mask] = i + 1
        return idx_mask

    def _clear_image(self):
        """
        clears image and all masks/logits for that image
        """
        self.image = None
        self.cur_mask_idx = 0
        self.frame_index = 0
        self.cur_masks = {}
        self.cur_logits = {}
        self.index_masks_all = []
        self.color_masks_all = []

    def reset(self):
        self._clear_image()
        self.sam_model.reset_state(self.inference_state)

    def set_img_dir(self, img_dir: str) -> int:
        self._clear_image()
        self.img_dir = img_dir
        self.img_paths = [
            f"{img_dir}/{p}" for p in sorted(os.listdir(img_dir)) if isimage(p)
        ]

        return len(self.img_paths)

    def set_input_image(self, i: int = 0) -> np.ndarray | None:
        guru.debug(f"Setting frame {i} / {len(self.img_paths)}")
        if i < 0 or i >= len(self.img_paths):
            return self.image
        self.clear_points()
        self.frame_index = i
        image = iio.imread(self.img_paths[i])
        self.image = image

        return image

    def get_sam_features(self) -> tuple[str, np.ndarray | None]:
        self.inference_state = self.sam_model.init_state(video_path=self.img_dir)
        self.sam_model.reset_state(self.inference_state)
        msg = (
            "SAM features extracted. "
            "Click points to update mask, and submit when ready to start tracking"
        )
        return msg, self.image

    def set_positive(self) -> str:
        self.cur_label_val = 1.0
        return "Selecting positive points. Submit the mask to start tracking"

    def set_negative(self) -> str:
        self.cur_label_val = 0.0
        return "Selecting negative points. Submit the mask to start tracking"

    def add_point(self, frame_idx, i, j):
        """
        get the index mask of the objects
        """
        self.selected_points.append([j, i])
        self.selected_labels.append(self.cur_label_val)
        # masks, scores, logits if we want to update the mask
        masks = self.get_sam_mask(
            frame_idx,
            np.array(self.selected_points, dtype=np.float32),
            np.array(self.selected_labels, dtype=np.int32),
        )
        mask = self.make_index_mask(masks)

        return mask

    def get_sam_mask(self, frame_idx, input_points, input_labels):
        """
        :param frame_idx int
        :param input_points (np array) (N, 2)
        :param input_labels (np array) (N,)
        return (H, W) mask, (H, W) logits
        """
        assert self.sam_model is not None

        with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
            _, out_obj_ids, out_mask_logits = self.sam_model.add_new_points_or_box(
                inference_state=self.inference_state,
                frame_idx=frame_idx,
                obj_id=self.cur_mask_idx,
                points=input_points,
                labels=input_labels,
            )

        return {
            out_obj_id: (out_mask_logits[i] > 0.0).squeeze().cpu().numpy()
            for i, out_obj_id in enumerate(out_obj_ids)
        }

    def run_tracker(self) -> tuple[str, str]:
        """
        After propagation, produce a list `self.color_masks_all` containing
        one 2-D uint8 mask (0/255) for every input frame, in order.
        """
        video_masks = {}  # frame_idx -> 2-D uint8 mask

        with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
            for frame_idx, obj_ids, mask_logits in self.sam_model.propagate_in_video(
                self.inference_state, start_frame_idx=0
            ):
                # → Boolean masks for every object in this frame
                bool_masks = [
                    (mask_logits[i] > 0.0).squeeze().cpu().numpy()
                    for i in range(len(obj_ids))
                ]

                # Collapse to a single binary mask, then convert to 0/255 uint8
                binary = np.any(bool_masks, axis=0)  # (H, W) bool
                grayscale = (~binary).astype(np.uint8) * 255  # (H, W) uint8

                video_masks[frame_idx] = grayscale

        # Store in chronological order
        self.color_masks_all = [video_masks[i] for i in sorted(video_masks)]

        msg = f"Created binary masks for {len(self.color_masks_all)} frames."
        instr = "Run `save_masks_to_dir()` to write them to disk."
        return "", f"{msg} {instr}"

    def save_masks_to_dir(self, output_dir: str) -> str:
        masks = getattr(self, "color_masks_all", None)

        if not masks or not isinstance(masks, list):
            warn = "No masks to export. Did you run tracking first?"
            guru.warning(warn)
            return warn

        # If ALL masks are empty (all zeros) bail early
        if not any(mask.any() for mask in masks):
            warn = "All masks are empty – nothing to save."
            guru.warning(warn)
            return warn

        os.makedirs(output_dir, exist_ok=True)
        guru.info(f"Saving masks to {output_dir}…")

        for img_path, mask in zip(self.img_paths, masks):
            if mask.ndim != 2:
                raise ValueError(f"Expected 2-D mask, got shape {mask.shape}")

            name = Path(img_path).stem
            out_path = Path(output_dir) / f"{name}.mask.png"

            # mask is already 0/255 uint8 – write as single-channel PNG
            iio.imwrite(out_path, mask)

        done = f"Saved {len(masks)} masks to {output_dir}!"
        guru.debug(done)
        return done


def isimage(p):
    ext = os.path.splitext(p.lower())[-1]
    return ext in [".png", ".jpg", ".jpeg"]


def draw_points(img, points, labels):
    out = img.copy()
    for p, label in zip(points, labels):
        x, y = int(p[0]), int(p[1])
        color = (0, 255, 0) if label == 1.0 else (255, 0, 0)
        out = cv2.circle(out, (x, y), 10, color, -1)
    return out


def get_hls_palette(
    n_colors: int,
    lightness: float = 0.5,
    saturation: float = 0.7,
) -> np.ndarray:
    """
    returns (n_colors, 3) tensor of colors,
        first is black and the rest are evenly spaced in HLS space
    """
    hues = np.linspace(0, 1, int(n_colors) + 1)[1:-1]  # (n_colors - 1)
    # hues = (hues + first_hue) % 1
    palette = [(0.0, 0.0, 0.0)] + [
        colorsys.hls_to_rgb(h_i, lightness, saturation) for h_i in hues
    ]
    return (255 * np.asarray(palette)).astype("uint8")


def colorize_masks(images, index_masks, fac: float = 0.5):
    max_idx = max([m.max() for m in index_masks])
    guru.debug(f"{max_idx=}")
    palette = get_hls_palette(max_idx + 1)
    color_masks = []
    out_frames = []
    for img, mask in zip(images, index_masks):
        clr_mask = palette[mask.astype("int")]
        color_masks.append(clr_mask)
        out_u = compose_img_mask(img, clr_mask, fac)
        out_frames.append(out_u)
    return out_frames, color_masks


def compose_img_mask(img, color_mask, fac: float = 0.5):
    out_f = fac * img / 255 + (1 - fac) * color_mask / 255
    out_u = (255 * out_f).astype("uint8")
    return out_u


def listdir(dir):
    if dir is not None and os.path.isdir(dir):
        return sorted(os.listdir(dir))
    return []


def make_demo(
    checkpoint_dir,
    model_cfg,
    root_dir,
    vid_name: str = "videos",
    img_name: str = "images",
    mask_name: str = "images",
):
    prompts = PromptGUI(checkpoint_dir, model_cfg)

    start_instructions = (
        "Select a video file to extract frames from, "
        "or select an image directory with frames already extracted."
    )
    vid_root, img_root = (f"{root_dir}/{vid_name}", f"{root_dir}/{img_name}")
    with gr.Blocks() as demo:
        instruction = gr.Textbox(
            start_instructions, label="Instruction", interactive=False
        )
        with gr.Row():
            root_dir_field = gr.Text(root_dir, label="Dataset root directory")
            vid_name_field = gr.Text(vid_name, label="Video subdirectory name")
            img_name_field = gr.Text(img_name, label="Image subdirectory name")
            mask_name_field = gr.Text(mask_name, label="Mask subdirectory name")
            seq_name_field = gr.Text(None, label="Sequence name", interactive=False)

        with gr.Row():
            with gr.Column():
                vid_files = listdir(vid_root)
                vid_files_field = gr.Dropdown(label="Video files", choices=vid_files)
                input_video_field = gr.Video(label="Input Video")

                with gr.Row():
                    start_time = gr.Number(0, label="Start time (s)")
                    end_time = gr.Number(0, label="End time (s)")
                    sel_fps = gr.Number(30, label="FPS")
                    sel_height = gr.Number(540, label="Height")
                    extract_button = gr.Button("Extract frames")

            with gr.Column():
                img_dirs = listdir(img_root)
                img_dirs_field = gr.Dropdown(
                    label="Image directories", choices=img_dirs
                )
                img_dir_field = gr.Text(
                    None, label="Input directory", interactive=False
                )
                frame_index = gr.Slider(
                    label="Frame index",
                    minimum=0,
                    maximum=len(prompts.img_paths) - 1,
                    value=0,
                    step=1,
                )
                sam_button = gr.Button("Get SAM features")
                reset_button = gr.Button("Reset")
                input_image = gr.Image(
                    prompts.set_input_image(0),
                    label="Input Frame",
                    every=1,
                )
                with gr.Row():
                    pos_button = gr.Button("Toggle positive")
                    neg_button = gr.Button("Toggle negative")
                clear_button = gr.Button("Clear points")

            with gr.Column():
                output_img = gr.Image(label="Current selection")
                add_button = gr.Button("Add new mask")
                submit_button = gr.Button("Submit mask for tracking")
                mask_dir_field = gr.Text(
                    None, label="Path to save masks", interactive=False
                )
                save_button = gr.Button("Save masks")

        def get_vid_dirs(root_dir, vid_name):
            vid_root = f"{root_dir}/{vid_name}"
            vid_paths = listdir(vid_root)
            guru.debug(f"Updating video paths: {vid_paths=}")
            return vid_paths

        def get_img_dirs(root_dir, img_name):
            img_root = f"{root_dir}/{img_name}"
            img_dirs = listdir(img_root)
            guru.debug(f"Updating img dirs: {img_dirs=}")
            return img_root, img_dirs

        def get_mask_dir(root_dir, mask_name, seq_name):
            return f"{root_dir}/{mask_name}/{seq_name}"

        def update_root_paths(root_dir, vid_name, img_name, mask_name, seq_name):
            return (
                get_vid_dirs(root_dir, vid_name),
                get_img_dirs(root_dir, img_name),
                get_mask_dir(root_dir, mask_name, seq_name),
            )

        def select_video(root_dir, vid_name, seq_file):
            seq_name = os.path.splitext(seq_file)[0]
            guru.debug(f"Selected video: {seq_file=}")
            vid_path = f"{root_dir}/{vid_name}/{seq_file}"
            return seq_name, vid_path

        def extract_frames(
            root_dir, vid_name, img_name, vid_file, start, end, fps, height, ext="png"
        ):
            seq_name = os.path.splitext(vid_file)[0]
            vid_path = f"{root_dir}/{vid_name}/{vid_file}"
            out_dir = f"{root_dir}/{img_name}/{seq_name}"
            guru.debug(f"Extracting frames to {out_dir}")
            os.makedirs(out_dir, exist_ok=True)

            def make_time(seconds):
                return datetime.time(
                    seconds // 3600, (seconds % 3600) // 60, seconds % 60
                )

            start_time = make_time(start).strftime("%H:%M:%S")
            end_time = make_time(end).strftime("%H:%M:%S")
            cmd = (
                f"ffmpeg -ss {start_time} -to {end_time} -i {vid_path} "
                f"-vf 'scale=-1:{height},fps={fps}' {out_dir}/%05d.{ext}"
            )
            print(cmd)
            subprocess.call(cmd, shell=True)
            img_root = f"{root_dir}/{img_name}"
            img_dirs = listdir(img_root)
            return out_dir, img_dirs

        def select_image_dir(root_dir, img_name, seq_name):
            img_dir = f"{root_dir}/{img_name}/{seq_name}"
            guru.debug(f"Selected image dir: {img_dir}")
            return seq_name, img_dir

        def update_image_dir(root_dir, img_name, seq_name):
            img_dir = f"{root_dir}/{img_name}/{seq_name}"
            num_imgs = prompts.set_img_dir(img_dir)
            slider = gr.Slider(minimum=0, maximum=num_imgs - 1, value=0, step=1)
            message = (
                f"Loaded {num_imgs} images from {img_dir}. Choose a frame to run SAM!"
            )
            return slider, message

        def get_select_coords(frame_idx, img, evt: gr.SelectData):
            i = evt.index[1]  # type: ignore
            j = evt.index[0]  # type: ignore
            index_mask = prompts.add_point(frame_idx, i, j)
            guru.debug(f"{index_mask.shape=}")
            palette = get_hls_palette(index_mask.max() + 1)
            color_mask = palette[index_mask]
            out_u = compose_img_mask(img, color_mask)
            out = draw_points(out_u, prompts.selected_points, prompts.selected_labels)
            return out

        # update the root directory
        # and associated video, image, and mask root directories
        root_dir_field.submit(
            update_root_paths,
            [
                root_dir_field,
                vid_name_field,
                img_name_field,
                mask_name_field,
                seq_name_field,
            ],
            outputs=[vid_files_field, img_dirs_field, mask_dir_field],
        )
        vid_name_field.submit(
            get_vid_dirs,
            [root_dir_field, vid_name_field],
            outputs=[vid_files_field],
        )
        img_name_field.submit(
            get_img_dirs,
            [root_dir_field, img_name_field],
            outputs=[img_dirs_field],
        )
        mask_name_field.submit(
            get_mask_dir,
            [root_dir_field, mask_name_field, seq_name_field],
            outputs=[mask_dir_field],
        )

        # selecting a video file
        vid_files_field.select(
            select_video,
            [root_dir_field, vid_name_field, vid_files_field],
            outputs=[seq_name_field, input_video_field],
        )

        # when the img_dir_field changes
        img_dir_field.change(
            update_image_dir,
            [root_dir_field, img_name_field, seq_name_field],
            [frame_index, instruction],
        )
        seq_name_field.change(
            get_mask_dir,
            [root_dir_field, mask_name_field, seq_name_field],
            outputs=[mask_dir_field],
        )

        # selecting an image directory
        img_dirs_field.select(
            select_image_dir,
            [root_dir_field, img_name_field, img_dirs_field],
            [seq_name_field, img_dir_field],
        )

        # extracting frames from video
        extract_button.click(
            extract_frames,
            [
                root_dir_field,
                vid_name_field,
                img_name_field,
                vid_files_field,
                start_time,
                end_time,
                sel_fps,
                sel_height,
            ],
            outputs=[img_dir_field, img_dirs_field],
        )

        frame_index.change(prompts.set_input_image, [frame_index], [input_image])
        input_image.select(get_select_coords, [frame_index, input_image], [output_img])

        sam_button.click(prompts.get_sam_features, outputs=[instruction, input_image])
        reset_button.click(prompts.reset)
        clear_button.click(prompts.clear_points, outputs=[output_img, instruction])
        pos_button.click(prompts.set_positive, outputs=[instruction])
        neg_button.click(prompts.set_negative, outputs=[instruction])

        add_button.click(prompts.add_new_mask, outputs=[output_img, instruction])
        submit_button.click(prompts.run_tracker, outputs=[instruction])
        save_button.click(
            prompts.save_masks_to_dir, [mask_dir_field], outputs=[instruction]
        )

    return demo


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument(
        "--checkpoint_dir", type=str, default="../checkpoints/sam2.1_hiera_large.pt"
    )
    parser.add_argument(
        "--model_cfg", type=str, default="configs/sam2.1/sam2.1_hiera_l.yaml"
    )
    parser.add_argument("--root_dir", type=str, required=True)
    parser.add_argument("--vid_name", type=str, default="videos")
    parser.add_argument("--img_name", type=str, default="images")
    parser.add_argument("--mask_name", type=str, default="images")
    args = parser.parse_args()

    # device = "cuda" if torch.cuda.is_available() else "cpu"

    print("Args before launch")
    print(args)

    demo = make_demo(
        args.checkpoint_dir,
        args.model_cfg,
        args.root_dir,
        args.vid_name,
        args.img_name,
        args.mask_name,
    )
    demo.launch(server_port=args.port)
