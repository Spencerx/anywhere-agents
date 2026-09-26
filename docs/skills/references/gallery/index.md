# Personal visual gallery

The [canonical gallery index](https://github.com/yzhao062/anywhere-agents/blob/main/skills/editable-figure/references/gallery/index.md) contains the selected paper and proposal overview figures, source records, and dated feedback. These are Yue Zhao's preferences; other users can treat the examples as candidates and record their own choices.

After cloning the repository or refreshing a consumer, open `references/gallery/index.html` inside the installed `editable-figure` skill to browse the images locally. The images and their original files are bundled with the skill, so this view works offline. The [feedback record](https://github.com/yzhao062/anywhere-agents/blob/main/skills/editable-figure/references/gallery/preferences.md) separates author statements from design interpretations.

## Grow the record through use

Follow the [canonical update procedure](https://github.com/yzhao062/anywhere-agents/blob/main/skills/editable-figure/references/gallery/index.md#grow-the-record-through-use). In a consumer, save feedback in `figure-preferences/index.md` at the consumer repository root or a persistent location named in local instructions. Installed skill copies are refreshed by bootstrap. Merge feedback into the source gallery before shared distribution, then regenerate its HTML with `scripts/build_gallery.py` and verify with `--check`.
