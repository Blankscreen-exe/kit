# rmbg

Remove the background from an image with a local AI model, and fix mistakes by painting over them on a local page.

## Usage

```
kit rmbg <image> [-o OUTPUT.png]               # find the subject and save it
kit rmbg <image> --edit [--no-open] [--window] # the same, then fix the result by painting
kit rmbg --setup                               # download the packages and model now
```

- Takes anything Pillow can read, such as JPG, PNG, WEBP, BMP and TIFF. Phone photos are turned the right way up.
- The result goes to `<name>-nobg.png` next to the image, or wherever `-o` points (`.png` or `.webp`). An existing
  file is never overwritten: the new one is saved as `<name>-nobg (2).png` and so on.
- `--edit` opens a page in your browser showing the result, with removed areas faded:
  - Paint **Remove** (red) over leftover background or an object you don't want, or **Keep** (green) over parts of the
    subject that went missing. A small rough stroke is enough: it spreads through similar connected colours and fits
    to the edges, and the preview updates a few seconds later. Only changes connected to a stroke are used, so the
    rest of the image stays as it was.
  - If a stroke spreads too far, paint Keep on what it shouldn't have taken; Keep also blocks Remove from spreading
    through it. Strongly different parts, such as a shine on a dark object, need their own stroke.
  - Keys: `X` swaps Remove and Keep, `[` and `]` change the brush size, `Ctrl+Z` undoes a stroke.
  - **Save** writes the full-size image. **Done** stops the page.
- Removed areas become transparent. Nothing is painted in to replace them.
- Hair, fur and glass are the hardest parts, close-ups can come out patchy, and strokes work poorly where the subject
  and background have similar colours.

The first run asks before downloading the model and the packages it needs (numpy, onnxruntime and opencv), about
370 MB on disk, so `kit hub` and scripts need `kit rmbg --setup` once first. They stay out of kit's shared environment, in
`%LOCALAPPDATA%\kit\rmbg` (`~/.local/share/kit/rmbg` on Linux, `KIT_RMBG_DATA` points somewhere else); delete that
folder to get the space back. After that, everything runs on this computer, on the CPU, and your images aren't sent anywhere.

Security: the page is only reachable from this computer and needs the one-time token in the address printed in the
terminal. Over SSH, `--edit` prints the `ssh -L` command to reach it from your own computer.

## Examples

```
kit rmbg photo.jpg                          # writes photo-nobg.png
kit rmbg photo.jpg -o cutouts\photo.webp
kit rmbg product.png --edit                 # check the result and paint any fixes
```
