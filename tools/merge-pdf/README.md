# merge-pdf

Merge images into a single PDF after arranging them on a local page with thumbnails.

## Usage

```
kit merge-pdf <images, folders or globs...> [-o OUTPUT.pdf] [-r] [--no-open] [--window]
kit merge-pdf <inputs...> --no-ui [--order name|date|given] [--page fit|a4|letter]
              [--orientation auto|portrait|landscape] [--margin none|small|medium] [--quality high|small]
```

- Takes JPG, PNG, WEBP, BMP, GIF (first frame) and TIFF (every page). Folders are scanned without subfolders
  unless you pass `-r`. Globs like `*.jpg` work in every shell. Other files are skipped with a warning.
- Opens a page in your browser (or its own app window with `--window`) showing one thumbnail per page,
  first sorted by file name (`img2` before `img10`).
  - Drag thumbnails to reorder them, or use the buttons on each card (move left/right, to start/end).
  - Rotate pages left or right, remove them (Undo brings them back), or sort by name, by date taken
    (the photo's EXIF date, else the file's modified time) or reverse the order.
  - Drop more image files onto the page, or use **+ Add images**.
  - Choose the page size (fit each image, A4 or Letter), orientation, margin, quality and file name,
    then click **Create PDF**. Open the PDF or its folder from the page, and click **Done** to stop.
- Keyboard: focus a page, then Alt+←/→ moves it, Alt+Home/End moves it to the start/end, R rotates right,
  Shift+R rotates left, Delete removes it, Ctrl+Z undoes a removal.
- Phone photos are turned the right way up using their EXIF orientation. Transparent areas become white.
- The PDF goes to `merged.pdf` in the current folder (or wherever `-o` points). An existing file is never
  overwritten: the new one is saved as `merged (2).pdf` and so on.
- `--no-ui` skips the page and builds the PDF straight away, for scripts.

Security: the page is only reachable from this computer and needs the one-time token in the address printed in the
terminal. It only reads the files you passed or dropped onto the page. Dropped files go to a temporary folder
that's deleted when merge-pdf stops (Done or Ctrl+C).

## Examples

```
kit merge-pdf .\scans                              # arrange every image in a folder
kit merge-pdf photo*.jpg -o trip.pdf                # arrange matching photos, save as trip.pdf
kit merge-pdf receipts --no-ui --page a4 --order date -o receipts.pdf
kit merge-pdf --window                             # start empty in an app window, then drop images in
```
