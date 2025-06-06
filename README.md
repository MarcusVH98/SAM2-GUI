# Changes made in this fork
- Masks output changed to be compatible with [Colmap mask_path](https://colmap.github.io/faq.html#mask-image-regions) masks.
- Skipping output of .mp4 for preview to save time.
- Updated model from Sam2 to 2.1

This repo is ment to be cloned into the root of sam2 repo to work.

# GUI for SAM2
A GUI tool for SAM2 video segmentation.

## Installation
You should install [SAM2](https://github.com/facebookresearch/segment-anything-2?tab=readme-ov-file) first.
```
pip install -r requirements.txt
```

## Processing Custom Data

We highly encourage users to structure their data directories in the following way:
```
- data_root
    '- videos
    |   - seq1.mp4
    |   - seq2.mp4
[and/or]
    '- images
    |   - seq1
    |   - seq2
    '- ...
```
## Usage
```
python mask_app.py --root_dir [data_root]
```
![gradio interface](asset/gradio_interface.png)

## Acknowledge
The app is modified based on [shape-of-motion](https://github.com/vye16/shape-of-motion/?tab=readme-ov-file).