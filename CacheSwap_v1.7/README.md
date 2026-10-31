# CacheSwap v1.7


Swap entries in the Roblox client cache (Windows) with a few clicks, using the method that worked in testing:
copy a **whole** cache entry (header + payload) over the target entry and update that entry's `size` row in
`rbx-storage.db`.

## Run

```
py -m pip install -r requirements.txt
py app.py
```

Single exe: double-click `build_exe.bat` (result is in `dist\CacheSwap.exe`).

The app looks in `%LOCALAPPDATA%\Roblox` by default (use **Change folder...** if yours differs).
Its own data (backups, originals, library, presets, names) lives in `%APPDATA%\CacheSwap`.

## Fleasion-derived UI and preview integration (v1.7)

- Ported Fleasion's `ThemeManager` palette to PySide6; choose System / Dark / Light from **View → Theme**.
- Added a Fleasion-style drag-and-drop field to the External assets tab. Drop a local file onto the field or enter a public Roblox asset ID / direct HTTPS URL.
- Added Fleasion's RBXM/RBXMX tree-and-properties widget to the side preview. Binary model decoding uses the included parser adapter; XML/RBXMX data is shown structurally when recognized.
- Added Fleasion's searchable JSON tree viewer instead of plain JSON text.
- Added Fleasion's font preview for TTF/OTF/TTC/WOFF/WOFF2-signature files.
- Added OBJ export for mesh payloads through Fleasion's mesh-processing code. Draco-compressed versions still require DracoPy.
- Extended image detection to BMP, GIF, WebP, TIFF, ICO, DDS and PSD; Pillow is used as a fallback decoder where supported.
- Added decoding of uncompressed 2D RGB/RGBA KTX1 and a structured TexturePack XML viewer showing referenced texture IDs and Fleasion-style global slots. KTX2 and block-compressed KTX1 still show metadata only.
- Kept CacheSwap's existing cache swap/restore/history logic and software-rendered R6/R15 animation preview.
- Included a regression fixture for `73885081417917`; its `Action4` priority, loop flag, short encoded duration, and large finite `LowerTorso.Z` value are checked without normalization.

The uploaded Fleasion `replacer_config.py` and full `cache_viewer.py` are coupled to additional modules not included in the supplied source archives (proxy tabs, config manager, cache manager, solid-model serializers/deserializers, OpenGL helpers, etc.). They are not imported wholesale in this version; the specific reusable widgets above are integrated to avoid breaking CacheSwap startup. RBXM binary preview uses the bundled parser adapter, so unsupported binary property types may be displayed only approximately. Fleasion-derived files are covered by GPL-3.0; see `THIRD_PARTY_NOTICES.md`.

## Preview pane

Click any entry (Cache or Learn tab) and the pane on the right shows what it is:

| Type | What you see |
|------|--------------|
| png / jpg / bmp / gif / webp / tiff / ico / dds / psd | the image on a checkerboard, with RGBA / R / G / B / A channel views (packed and normal maps look like noise in RGBA) |
| mesh (Roblox mesh v1 - v7; v7 needs DracoPy) | a shaded 3D model: drag to rotate, wheel to zoom, wireframe toggle |
| ogg / mp3 | Play / Stop buttons |
| KeyframeSequence | live R15/R6 box-rig preview using Pose CFrames, keyframe interpolation and playback controls |
| CurveAnimation | live R15/R6 box-rig preview with play/pause, timeline scrubbing, loop handling, speed control, orbit/zoom, wireframe and grid |
| KTX1 uncompressed RGB/RGBA | rendered image; other KTX layouts show metadata |
| JSON | searchable, expandable tree viewer |
| RBXM / RBXMX | structured instance tree and property table |
| Font signatures (TTF/OTF/TTC; WOFF/WOFF2 detected) | font-family/sample preview when supported by the Qt font backend |
| TexturePack XML | referenced texture IDs and global map slots |
| unknown / other | basic information and header bytes |

Partially supported: KTX1 uncompressed RGB/RGBA is decoded; KTX2 and block-compressed KTX layouts still show metadata only. Avatar-specific meshes/clothing are not reconstructed into a full Roblox avatar. Both `CurveAnimation` and legacy `KeyframeSequence` can be previewed on a simplified software-rendered box rig; it is not a pixel-perfect Roblox avatar or a substitute for Roblox's own animation blending/interpolation. Legacy poses use linear position blending and quaternion slerp. Curve key times use 14400 ticks per second, matching Fleasion's current `animation_viewer.py` parser. Large finite key values are preserved; the simplified viewer may not frame an extreme translation. Animation chunks compressed with LZ4 or zstd are both attempted; an unreadable animation is shown with an error instead of crashing the app. Entries without an RBXH header are shown as raw files (status NO-RBXH).
Anything the preview can't read shows a short message instead of crashing. Previews only read files.

## v1.6 additions

- Corrected `CurveAnimation` tick conversion to 14,400 ticks/second to match Fleasion's current parser.
- Large finite `FloatCurve` values are retained; magnitude alone no longer makes the parser drop a track.
- Short-duration messaging is diagnostic, not a declaration of corruption.

## v1.5 additions

- **External assets tab**: import one or more local files, or download a public Roblox asset by numeric ID / `rbxassetid://...`, or fetch a direct HTTPS file URL.
- External sources are copied into `%APPDATA%\CacheSwap\external_sources` with a persistent index and content-based deduplication. They remain available after closing the application.
- Select an external asset to preview it before applying it. The preview supports the same images, meshes, audio, `CurveAnimation`, and `KeyframeSequence` formats as the Cache tab. **Import selected -> target** applies the source to the cache target selected in the Cache tab.
- Downloads use HTTPS, have a 100 MB limit, do not use browser cookies/login sessions, and reject obvious local/private URL targets. Private Roblox assets may not be downloadable without access; CacheSwap will not try to read browser login cookies.
- This is an **offline import into a local cache entry**, not a live redirect from a Roblox asset ID to an external URL like Fleasion's proxy mode.

## v1.4 additions

- **Animation rig viewer**: plays `CurveAnimation` (`Vector3Curve`, `EulerRotationCurve`, `RotationCurve`) and legacy `KeyframeSequence` Pose CFrames on a simplified software-rendered R15/R6 rig. Includes play/pause, scrubbing, loop support, playback speed, orbit/zoom, wireframe and grid controls.
- **Animation parser tests** cover `ValuesAndTimes`, interpolation, parent-folder mapping, legacy keyframes, CFrame special rotation IDs and pose interpolation using generated sample files.
- The project is licensed under **GPL-3.0**; see `LICENSE` and `THIRD_PARTY_NOTICES.md`.

## v1.3 additions

- **Type sidebar** (left of the Cache table) with counts per kind.
- **Replace with...** (button / right-click): pick another cache entry or a file. You can also **drop a file onto a row**.
- **Replacements tab**: every active replacement with what replaced it, where it came from, when, and whether
  Roblox has reverted it; per-item **Revert**; a persistent history of all swaps and restores.
- **Profiles** (Presets & profiles tab): save all active replacements as a set; export / import a `.cswap` file
  (it contains the replacement files, so it works on another PC).
- **Export entry...**: copies the raw cache file plus a `.txt` report (header bytes, db size, kind). Use it to send
  me an entry that the preview rejects.
- Import / drop accepts any file type now, not only animations (a mismatch with the target's type shows a warning).

## Typical use: make `/e wave` play another animation

1. **Close Roblox completely** (tray icon too). The app refuses to swap while it is running.
2. **Learn tab** -> *Start Learn*. Launch Roblox, join an experience that loads your animation, press
   *I joined*, play it once, press *I played it*, close Roblox, *Stop & show results*.
   The **Asset ID** column shows the asset each new entry belongs to (read from the entry header URL).
3. Select the entry for your animation -> **Use as source**.
4. **Cache tab** -> search `47a6a89e49946fbfd8d2e1d607ff13bf` (Wave), select it -> **Set as target**.
   (Right-click -> *Name...* to label entries.)
5. **Swap**. Launch Roblox, join anything, type `/e wave`.
6. **Presets tab** -> *Save current target/source as preset* to redo it in one click later.

**Restore target / Restore ALL** put the saved originals back.

## Importing a local / external asset

For a local file or downloaded direct URL, open **External assets** -> **Add local file(s)…** or **Download URL / asset ID…**. Select the row to preview it, then set a target in the Cache tab and click **Import selected -> target…**. Files are saved in the external source library so you can reuse them later.

For a one-time local import you can still use the Cache tab's **Import file -> target…**. If you have an animation as a binary `.rbxm` file (starts with `<roblox!`), you can put it straight into a slot:

1. Select the slot (for example Wave) and click **Set as target**.
2. Click **Import file -> target...** and choose the file.
3. The app builds the entry from the target's own header plus your file, fixes the header's length field, and swaps it in like any other source.

The length field's position is detected from your real cache entries (**Probe header** shows what it found
and saves `header_probe.txt` in the app folder; it is also copied to the clipboard). If the layout can't be
worked out, the import is refused instead of guessing.

Roblox rejected payload-only edits in an earlier test and the reason was never found, so this is
experimental. If `/e wave` is unchanged afterwards the entry was rewritten: the row shows `REVERTED`.
Newer animations use `CurveAnimation` instead of `KeyframeSequence` (shown as kind `curve-anim`); swapping
one into a `KeyframeSequence` slot is untested.

## Supplied animation sample

The supplied `ImportedAnimationClip3_73885081417917.rbxm` is a looped `CurveAnimation` with `Action4` priority and curves under `HumanoidRootPart/LowerTorso`. CacheSwap preserves the serialized finite curve values, including large values, rather than deciding they are corrupt based on magnitude alone. The preview uses the time base from Fleasion's current parser (14,400 ticks per second) and reports the serialized duration without modifying it.

## Safety

- Never runs while Roblox is running; refuses duplicate/invalid IDs.
- Backs up the database (sqlite backup API, newest 15 kept) before every swap.
- Only ever updates one `size` cell per swap. The database is never deleted or rewritten otherwise.
- Keeps the target's original until you restore it. If Roblox rewrites the entry itself, the row shows
  `REVERTED` and the next swap saves the fresh original.
- Every source is copied to the app's library, so swaps still work after Roblox drops the source from its cache.

## Known limits

- Whole-entry swap is proven for Jump -> Wave. The supplied `73885081417917` file parses as a looped `CurveAnimation`; import into Wave and playback in the normal Roblox client are still unverified. Cross-swapping a `CurveAnimation` into a `KeyframeSequence` slot may not work.
- Roblox can refresh entries at any time or change the cache format in an update.
- Changes are local to your PC. Editing the client cache is not officially supported by Roblox;
  use at your own risk.

## Tests

`py test_core.py` runs the core logic against a synthetic cache (no Roblox needed).
`py test_preview.py` tests the preview parsers and pane (synthetic meshes, images and animations; requires PySide6, numpy, DracoPy, lz4 and zstandard).
`py test_animation_data.py` tests animation parsing and interpolation without opening a GUI.
`py test_supplied_animation.py` checks the supplied `73885081417917` file and asserts its serialized duration and large finite curve value remain unchanged.
`py -m unittest -v test_format_detection test_extended_formats` tests common image signatures, KTX1 RGB/RGBA decoding, and TexturePack ID extraction.
