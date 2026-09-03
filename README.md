# AREPO Camera Lab

`arepo-camera-lab` is a local, interactive camera and physical-channel explorer
for portable AREPO full-cell scenes. The live browser controls can display
native Voronoi volume rendering on the Mac's Metal GPU, explicit cell faces
through VTK, or the existing fast WebGL point preview. The separate VTK
point/glyph explorer remains available.

Both native views use AREPO-VTK's stored neighbour planes. The volume view
integrates physical path lengths through cells with an illustrative display
transfer; the face view exposes their polyhedral geometry. The existing
ArepoVTK-stellar/ArepoRT production optical model remains separate.

Development follows [document-as-you-build guidance](CONTRIBUTING.md):
usage, implementation decisions, verification, and remaining work stay current
with each change.

## Quick start

```bash
git clone https://github.com/aayushdesai/arepo-camera-lab.git
cd arepo-camera-lab
conda env create -f environment.yml
conda activate arepo-camera-lab

# Open a deterministic synthetic disk and bipolar-outflow example.
arepo-camera-lab demo
```

For reviewed camera-knot epochs, use a verified snapshot catalog. The dropdown
contains only entries with both a hash-bound portable scene and a hash-bound
physical-field sidecar:

```bash
arepo-camera-lab serve \
  --catalog /absolute/path/to/camera_lab_catalog.json \
  --pose-bundle '/absolute/path/to/stellar_camera_poses.json' \
  --pose-bundle-sha256 <trusted-pose-bundle-sha256> \
  --snapshot 721 \
  --max-points 400000 \
  --cache-directory ~/.cache/arepo-camera-lab \
  --session-directory ~/camera-lab-poses
```

The snapshot control is a dropdown, not a free-running number. Selecting a row
reuses its verified local scene/sidecar pair, or downloads and verifies that
pair once if it is missing, before the visible-data label changes.
A catalog has a global `required_auxiliary_fields` contract, so an
epoch missing magnetic field, pressure, or sound speed cannot silently appear
with fewer controls. See `examples/catalog.example.json`.

For an automatically archived disposable cache, add
`--cleanup-on-close --sync-back-destination user@login:/unique/session/path`.
The **Archive & close** button freezes edits, saves the browser session, and
stops the native worker. It archives session outputs, verifies the archive and
cluster inputs, removes only verified local cache files, and then stops the
server after reporting completion. Closing a browser tab alone deliberately
does not delete data because reloads and accidental tab closes are ambiguous.

For one ad-hoc local file, `serve --scene ... --field-sidecar ... --snapshot
721` remains available, but it does not create a multi-epoch dropdown.

### Native Voronoi volume, faces, and measurements

On macOS, the live viewer initially selects **Native cell volume · Metal**.
It traverses the full native Voronoi connectivity on the local GPU and integrates
inside cells. **Native cell faces · VTK** retains the explicit polyhedra,
optional edges, and interior faces for geometry debugging. The point-budget
selector affects only the point preview. Both native modes need complete v052
connectivity; the Metal view also requires the Xcode Command Line Tools.

Choose a **Volume transparency** preset for the **Disk**, **Through to the
remnant**, or **Diffuse outflow**. These change density visibility and optical
weighting. The field colour, camera, and original pose remain independent.
**Hide cells below density** starts at 100 g cm^-3; the outflow preset lowers it
to 0.01 g cm^-3. Advanced controls expose reference density, physical path
length, and density weighting. These are display settings, not calibrated
radiation transport. A fresh viewer selects **Continuous field** and **Standard**
quality. It blends local gradients across native cell boundaries and computes
colour and opacity from reconstructed fields. **Linear field · fast**,
**Original cell values**, and **Legacy smoothing** remain selectable. Saved
presets retain their previous reconstruction and transfer order. These display
reconstructions do not add simulation resolution or establish whether a feature
is physically resolved.
The gas-density channel
is the simulation mass density in g cm^-3, not a density proxy.

Neither native view changes the run's refinement. The
[snapshot 721 geometry audit](docs/mesh-fidelity-audit.md) matches all generator
positions and checks sampled cell volumes against the raw snapshot. This
validates the saved geometry at that epoch; the image-quality problem remains.

Continuous reconstruction uses two samples per crossed cell. **Standard**
uses one ray per pixel; **High · antialiased** uses four and takes longer.
Orbit/zoom uses a smaller, fast linear preview and then restores the selected
reconstruction after interaction stops. Gradients are prepared once per field
change on the M4 and reused for camera changes. At 960 by 640 with one ray and
two cell samples, the first continuous comparisons took 1.83–4.32 s on M4 Pro;
linear reconstruction took 0.06–0.11 s. These exclude loading and image delivery.

Under **Adjust transparency**, **Compute opacity from reconstructed density**
controls transfer order. **Keep opacity outside the colour range** clamps the
palette without hiding that gas. The optional dense-gas fade reduces foreground
opacity over one decade in density; a fraction of 1 leaves it unchanged. These
settings are saved with presets. See the [field reconstruction notes](docs/field-reconstruction-work.md)
for the comparison, limitations, and source-backed method.

**Fit visible mesh** frames the visible cells without editing an imported
camera alternative. Orbit, pan, deep zoom, all physical/derived channels,
palettes, ranges, gamma, saturation, brightness, opacity, presets, and pose
review retain their controls. Point size applies to the point preview. Native
view and volume settings are saved with style presets and session archives.

The bottom-right overlay shows the displayed snapshot's simulation time and
index, a calibrated scale bar, and the field-colour legend. Choose centimetres,
kilometres, or automatic units. The two-click ruler measures **3D surface**
distance in the face view. Volume and point views measure **projected** distance
in the camera plane, because a transparent volume has no unique surface.
Measurements retain their coordinate convention, scene hash, and camera plane;
face measurements also retain native cell IDs.

The native worker is owned by the local server. **Quit**, Ctrl-C, and successful
archive shutdown stop it locally, including an active compiler or render.
Quitting does not require a cluster connection. Volume camera changes reuse
resident geometry and field buffers; surface visibility changes rebuild faces.

To save annotated PNGs and exact camera/style/hash records:

```bash
arepo-camera-lab capture-mesh \
  --config /absolute/path/native_capture.json \
  --output-directory /absolute/path/native-preview-v002
```

See the [native viewer notes](docs/voronoi-raytrace-backend.md),
[volume capture example](examples/native-volume-capture.example.json), and
[face capture example](examples/native-capture.example.json). Existing
`capture-gallery` and `capture-spline-movie` retain their point-rendering
behavior. A native-volume movie export has not been added to those commands.

This branch reads prepared v052 scenes and ID-bound sidecars. Direct loading
of arbitrary raw HDF5 snapshots through AREPO-VTK is not implemented.

For the native VTK desktop explorer, including magnetic-vector glyphs when a
field sidecar is supplied:

```bash
arepo-camera-lab vtk \
  --rsync-scene user@login.example:/absolute/path/to/scene_v052.bin \
  --scene-sha256 <trusted-scene-sha256> \
  --field-sidecar /absolute/path/to/snapshot_0721.fields.npz \
  --snapshot 721 \
  --max-points 400000 \
  --cache-directory ~/.cache/arepo-camera-lab/scenes \
  --channel rotational_fraction
```

Native VTK controls:

- `[` / `]`: previous or next physical channel;
- `L`: linear, log10, or symmetric-log scaling;
- `P`: cycle the eight palettes;
- `1`, `2`, `3`: 1-99%, 5-95%, or full scalar range;
- `C`: clip or retain cells outside the selected color range;
- `B`: show or hide sampled magnetic-vector glyphs;
- `K`: save a no-clobber camera-pose JSON file;
- mouse: native VTK orbit, pan, and unrestricted zoom.

Add `--show-magnetic-glyphs` to open with the vector arrows already visible.

Use `--max-points 0` to pass all cells to VTK. This is intentionally not
subject to a browser-memory limit, although the computer still needs enough
RAM and GPU memory for the selected arrays.
Large remote scenes should use `--rsync-scene`, not SSHFS. The command transfers
only the requested `scene_v052.bin` into a content-addressed local cache, shows
rsync progress, supports resuming an interrupted `.rsync-partial`, and verifies
the complete file against `--scene-sha256` before loading it. Subsequent launches
reuse the verified cache. It does not download every simulation snapshot.

Catalog mode applies the same resumable, checksum-verified rsync behavior to
both the selected scene and its physical-field sidecar. It downloads only the
selected dropdown epoch, not the entire simulation.

For a scene already stored on a local filesystem, use `--scene /local/path`.
The optional `--cache-directory` with `--scene` performs a verified local copy;
it is not needed for ordinary local files.

Immutable scenes with an already verified manifest digest can skip a second
full-file hash pass by adding `--scene-sha256 <digest>`. Files loaded from the
toolbar are hashed normally.

The server listens only on `127.0.0.1`. Open the printed `http://127.0.0.1`
URL; opening the control shell as a `file://` page cannot reach the local API.
`400000` remains a fast default, but there is no artificial upper point limit:
enter `0` to load every cell in the scene. The status band reports each loading
phase and progress, and the visible-data label always states which simulation
output supplied the cells currently visible.

The display panel keeps the underlying channel values rather than baking in one
normalization. For each channel you can choose linear, log10, or symmetric-log
scaling; edit the numeric minimum and maximum; apply percentile presets; choose
from eight color maps; invert the map; and tune gamma, saturation, brightness,
point size, and opacity. The color range and units are shown beside the control.
The minimum, maximum, and symmetric-log threshold use a selectable 4, 6, 8, or
12-significant-digit display precision. This only controls formatting: focusing
an input reveals its full stored value, and rendering retains full floating-point
precision until the value is explicitly edited.

The browser can also define derived channels without rebuilding a scene. Enter
a stable channel name and a formula, or insert fields from the selector. The
safe expression language supports `+`, `-`, `*`, `/`, `^`, parentheses, and
`abs`, `sqrt`, `log10`, `ln`, `exp`, `min`, `max`, `pow`, and `clip`. For
example, `gas_pressure / magnetic_pressure` reproduces the built-in
dimensionless `plasma_beta` channel. Formula definitions are stored in the
browser and recomputed from the newly loaded arrays whenever the catalog
snapshot changes. Unknown fields and cyclic formulas are rejected; divide by
zero and other non-finite results are counted and hidden from the point render.

### Review immutable camera alternatives

`--pose-bundle` accepts the original `stellar_camera_keyframes_v001` format or
the reviewed `stellar_camera_review_bundle_v002` format. The source geometry is
immutable: every entry in `alternatives` and every original `pose_id` remains
present. The grouped selector lists every alternative at every simulation
snapshot. Choosing a pose at another snapshot invokes the verified catalog
loader, waits for that exact scene/sidecar pair, then restores its look-at,
source position provenance, direction, up vector, and screen half extent.

Historical v001 files did not record display styling. The viewer says this
explicitly and starts from named runtime defaults; it never invents a recovered
style. To review quickly:

1. restore any exact pose and tune the physical channel, scale/range,
   copper-blue palette, gamma, saturation, brightness, point size, and opacity;
2. copy that state to a named, immutable per-channel preset revision;
3. append bindings for the selected pose or all alternatives;
4. add a full per-pose override only where a composition needs different
   styling; and
5. download the combined v002 bundle or save it no-clobber under
   `--session-directory`.

The review state records exact low/high values, symmetric-log threshold,
palette inversion, all display controls, point budget, canvas dimensions, and
scene/sidecar hashes. An amber status identifies a browser-retained unsaved
draft. The verification panel reports the active immutable camera and exact
display values. Applying a preset or override appends a binding and never
rewrites camera geometry.

Compile a finished review into backend-neutral intent rows and native config
overlays without changing any camera geometry:

```bash
arepo-camera-lab compile-render-intent \
  --review-bundle /absolute/path/stellar_camera_review_bundle_v002.json \
  --output-directory /absolute/path/render-intents-v001
```

Every pose must have a saved style binding. The compiler keeps WebGL-only point
size, opacity, and point budget separate from the shared camera, scalar range,
palette, gamma, saturation, and brightness contract. Output is no-clobber and
includes a SHA-256 manifest.

### Batch-capture saved alternatives

Render every no-clobber saved camera alternative through the same WebGL point
renderer, with all 24 physical channels and one frozen copper-blue scale per
channel:

```bash
conda run --no-capture-output -n arepo-camera-lab arepo-camera-lab capture-gallery \
  --poses ~/Downloads/stellar_camera_poses.json \
  --catalog ~/Movies/AREPO/arepo_camera_lab_verified_catalogs/phase21_keyframes_10epoch_da7ea20_63858030.json \
  --output-directory ~/Movies/AREPO/camera_pose_gallery/webgl \
  --max-points 1000000
```

The command uses the system Google Chrome in headless WebGL mode and the Node
runtime already present on macOS. It loads one simulation epoch at a time,
applies each exact saved physical pose bound to that epoch, and writes a PNG for
every pose/channel pair plus checksums, exact capture-state records, frozen
ranges, and an `index.html`. It refuses to overwrite the output directory.

A completely self-contained HTML file can also be built:

```bash
arepo-camera-lab build \
  --scene /absolute/path/to/scene_v052.bin \
  --snapshot 721 \
  --max-points 700000 \
  --output snapshot_0721_camera_lab.html
```

## Camera controls

- Drag: orbit.
- Shift-drag or right-drag: pan the look-at point.
- Wheel or the zoom buttons: change orthographic half extent.
- Double-click: recenter on a visible feature and move inward by 2.86x.
- `K`: append the current style as a per-pose override when reviewing an
  imported immutable pose bundle.
- `Space`: play a compiled camera path when one is embedded.
- `R`: reset the camera.

The deep-zoom floor is one millionth of the scene display radius. A live readout
shows the physical screen half extent in centimeters, making it possible to
move from the full remnant toward white-dwarf scales.

## Physical channels

The current browser view derives these channels from cell position, density,
temperature, and velocity:

- density and temperature;
- total speed and radial velocity;
- signed azimuthal velocity and rotational fraction;
- angular-momentum alignment;
- signed outward axial velocity and an outward mass-flux proxy;
- cylindrical radius and signed axial position.

These are exploratory color mappings, not synthetic observables. Particle ID is
used for deterministic sampling rather than color.

Magnetic and thermodynamic extensions are intentionally schema-bound. See
[docs/physical-channels.md](docs/physical-channels.md) for useful B-field,
pressure, entropy, and dimensionless-ratio controls and the source fields they
require.

### Magnetic, pressure, and entropy fields

Portable v052 scenes do not contain magnetic field, pressure, or entropy. The
viewer therefore never invents those quantities. Build an explicit companion
file from the matching raw HDF5 snapshot, with unit conversions supplied by the
user:

```bash
arepo-camera-lab fields \
  --snapshot /absolute/path/to/snapshot_0721.hdf5 \
  --output snapshot_0721.fields.npz \
  --magnetic-dataset PartType0/MagneticField \
  --magnetic-unit-gauss <code-B-to-gauss> \
  --pressure-dataset PartType0/Pressure \
  --pressure-unit-dyn-cm2 <code-pressure-to-cgs> \
  --entropy-dataset PartType0/Entropy \
  --entropy-unit-cgs <code-entropy-to-cgs>

arepo-camera-lab serve \
  --scene /absolute/path/to/scene_v052.bin \
  --field-sidecar snapshot_0721.fields.npz \
  --snapshot 721
```

Dataset names are explicit because AREPO outputs differ by configuration. Unit
factors are mandatory. The sidecar is joined to the scene by stable particle ID
and rejected if any displayed cell is missing or duplicated. A valid magnetic
sidecar adds `|B|`, signed axial and azimuthal field, magnetic pressure, Alfvén
speed, field/velocity alignment, toroidal and poloidal fractions, plasma beta,
gas pressure, the derived `P/rho^(5/3)` entropy proxy, sound speed, and Mach
number. These channels retain their physical arrays and can use the same
copper-blue palette and frozen range contract as the native Voronoi renderer.

## Camera poses and spline paths

An **AREPO snapshot index** is the simulation output number: index `721` means
the data in `snapshot_0721.hdf5`. A **camera pose** is only the selected look-at
point, orientation, roll, and orthographic half extent associated with that
output. These are separate concepts throughout the UI.

The v002 review workflow keeps the imported alternatives immutable and stores
style drafts in browser local storage. Explicit saves append preset revisions
and pose-style bindings; the server writes numbered v002 bundles under
`--session-directory`. The original v001 `keyframes` list remains available to
the spline compiler, while `alternatives` retains all camera choices. The point
budget controls cell detail only; it has no effect on the number or smoothness
of spline knots.

The spline does not select which simulation outputs will be rendered. Final
production can render every available AREPO snapshot. It only needs a sparse
set of **camera-knot epochs** where a human specifies the desired pose; the
camera between knots is solved continuously. Start with the first and last
output plus physical transitions, then add another knot only where the previewed
spline needs correction. For the present merger-to-outflow sequence, useful
initial knot epochs are roughly `31, 201, 421, 561 or 641, 721, 821, 901, 957,
1016`. Multiple poses at one epoch are alternatives for review, because the
final camera is a single-valued function of snapshot/time and the compiler
rejects duplicate knot indices.

For a bundle with several alternatives, first create reviewable route inputs
without changing the downloaded file:

```bash
arepo-camera-lab routes \
  --poses stellar_camera_poses.json \
  --output-directory camera-routes \
  --conflict 820 821
```

The route command preserves the exact latest-pose sequence as a diagnostic and
writes two smoothest continuous candidates, one omitting snapshot 820 and one
omitting snapshot 821. This makes an incompatible adjacent-pose choice explicit
instead of hiding a large one-frame spin inside a nominally smooth spline.

Save poses from at least two epochs, then compile them against an existing
21-column `v055` timeline/template:

```bash
arepo-camera-lab spline \
  --poses stellar_camera_poses.json \
  --template accepted_camera_path.tsv \
  --output camera_spline.tsv \
  --diagnostics camera_spline_diagnostics.tsv
```

The compiler uses a cubic Hermite spline for the look-at point, shortest-arc
eased quaternion SLERP for orientation, and a shape-preserving log-space spline
for zoom. The eased SLERP stops cleanly at a reviewed knot and cannot introduce
the large SQUAD overshoot that is possible between widely separated poses.
`--orientation-mode squad` remains available for closely spaced poses. Both
modes produce a continuously moving camera rather than a hard cut.

Saved alternatives are independent viewpoints, not an ordered orbit. For a
camera that travels in one direction around the evolving physical axis, select
and compile the orbit explicitly:

```bash
arepo-camera-lab orbit \
  --poses stellar_camera_poses.json \
  --template accepted_camera_path.tsv \
  --omit 821 \
  --direction positive \
  --output-route reviewed_orbit.json \
  --output reviewed_orbit.camera_path_v055.tsv \
  --diagnostics reviewed_orbit.diagnostics.tsv
```

The orbit compiler keeps the accepted look-at and framing as its composition
reference, chooses exact saved alternatives with monotonically unwrapped
orbital phase, and aligns the horizon with the transported physical axis.
Diagnostics include orbital phase and its signed per-frame increment; a phase
reversal is a hard failure. The legacy shortest-arc spline remains unchanged.

## Scene format

Version `0.4` reads portable `ARVTKSTARV052A` full-cell scenes containing cell
position, density, temperature, velocity, and stable particle ID. This is a
deliberately explicit contract. Raw AREPO HDF5 snapshots vary in field names,
unit metadata, and temperature conversion, so the project does not guess those
semantics. A future HDF5 adapter should require an explicit field and unit map.

The data objects are deliberately separate:

1. `snapshot_0721.hdf5` is the original simulation output.
2. `scene_v052.bin` is an ArepoVTK-stellar/ArepoRT export of reusable cell data
   from that output. It contains cell centers, density, temperature, velocity,
   stable IDs, and the scene metadata needed by the explorers.
3. `snapshot_0721.fields.npz` is an optional particle-ID-matched sidecar for
   fields not present in v052, such as magnetic field, pressure, and sound speed.
4. A camera-pose JSON stores a camera only. It contains no simulation cells.
5. ArepoRT consumes the accepted camera and transfer settings for exact Voronoi
   ray traversal and production images.

## Native rendering and production movies

The point views are scouting renderers. VTK adds explicit native cell faces
and magnetic-vector glyphs. The Metal companion now integrates rays directly
through the native cells for local volume previews. Its illustrative opacity
model is separate from the production ArepoRT optical profiles. See
[the backend notes](docs/voronoi-raytrace-backend.md) for the geometry, transfer,
measurement, and verification contracts.

## Development

```bash
python -m unittest discover -s tests -v
```

### Reviewed point-cloud spline preview

Render a bounded local movie that follows every `N`th row of an accepted v055
camera spline. Camera motion is continuous; the visible simulation state is the
latest verified catalog snapshot at or before that camera row, so sparse epochs
are never presented as a continuously exported simulation. Display values are
smoothly interpolated between the reviewed route poses.

Each captured frame records camera time separately from the visible simulation
snapshot. The frame plan binds the latter to exact catalog scene and field
sidecar SHA-256 values. Interactive saved-pose loading still requires the pose's
own snapshot and scene hash; spline capture does not weaken that guard.

```bash
conda run --no-capture-output -n arepo-camera-lab arepo-camera-lab \
  capture-spline-movie \
  --camera-path /absolute/path/to/camera_path_v055.tsv \
  --route /absolute/path/to/route.json \
  --reviewed-bundle /absolute/path/to/stellar_camera_review_bundle_v002.json \
  --reviewed-bundle-sha256 EXPECTED_SHA256 \
  --catalog /absolute/path/to/verified_catalog.json \
  --output-directory /absolute/new/output/directory \
  --frame-step 5 --max-points 500000 --width 1280 --height 720 \
  --cleanup-cache-after
```

The product includes the MP4, every source PNG, a camera/visible-snapshot
timeline, browser capture records, and a checksum manifest. The cleanup flag
removes only verified content-addressed scene/field cache files after the movie
and `ffprobe` checks pass.

On an eta compute node where the catalog's NFS sources are already mounted, use
`--direct-catalog-inputs` instead of transferring a local cache. The command
verifies the catalog SHA-256 values in place. Do not combine it with
`--cleanup-cache-after`; the direct NFS source products are immutable inputs and
must never be deleted.

The project uses no-clobber output behavior for generated scenes, HTML files,
camera paths, and diagnostics.

## Archive and clear a local session

Large caches are disposable only after their immutable cluster originals and
the uploaded session outputs are verified. The cleanup command checks both
local and remote input SHA-256 values, creates a unique remote destination,
rsyncs the pose directory with checksums, verifies a dry-run is empty, and only
then deletes the named local cache files:

```bash
arepo-camera-lab cleanup \
  --outputs-directory ~/camera-lab-poses \
  --sync-back-destination user@login:/cluster/camera-lab-sessions/session-001 \
  --cached-scene ~/.cache/arepo-camera-lab/scenes/scene_v052_<hash>.bin \
  --remote-scene-source user@login:/cluster/scenes/snapshot_0721/scene_v052.bin \
  --scene-sha256 <scene-sha256> \
  --cached-field-sidecar ~/.cache/arepo-camera-lab/fields/snapshot_0721.fields_<hash>.npz \
  --remote-field-sidecar-source user@login:/cluster/fields/snapshot_0721.fields.npz \
  --field-sidecar-sha256 <sidecar-sha256>
```

For a multi-snapshot server, clean every verified cache currently present from
one catalog instead of listing files individually:

```bash
arepo-camera-lab cleanup \
  --outputs-directory ~/camera-lab-poses \
  --sync-back-destination user@login:/cluster/camera-lab-sessions/session-002 \
  --catalog /absolute/path/verified_catalog.json \
  --cache-directory ~/.cache/arepo-camera-lab
```

The server has separate **Quit** and **Archive & close** actions. **Quit** (or
Ctrl-C) stops the local Python server immediately, retaining files and browser
drafts. It never contacts the cluster.

**Archive & close** saves a separate browser-session record containing all camera
alternatives, named styles, per-pose drafts, formulas, and the current view. It
then checks the cluster copies of the managed caches, rsyncs only recognized
camera/session/review products, verifies the transfer, and removes the unchanged
content-addressed cache files. Camera bundles and unrelated local files remain.
Progress and failures stay visible. The browser acknowledges the completion
receipt before shutdown; a closed browser cannot prevent the worker finishing.
After the server stops, the page attempts to close automatically. If the browser
keeps the tab open, **Close tab** or the browser controls close it.

Supplying `--sync-back-destination user@login:/archive/parent` enables the archive
button without also requiring `--cleanup-on-close`. Every attempt creates a fresh
`archive_<timestamp>_<id>` child, so failed attempts remain available and retries
do not collide. A session can retain its destination in `archive_settings.json`:

```json
{
  "schema": "arepo_camera_lab_archive_settings_v001",
  "destination": "user@login:/archive/parent"
}
```

The explicit command-line destination takes precedence over saved settings.
Completed and failed staging records are preserved under the session's
`.archives` directory; completion receipts remain at the session root.

The native VTK command can perform the same operation when its window closes by
adding `--cleanup-on-close --sync-back-destination user@login:/unique/path` and
using both `--rsync-scene` and `--rsync-field-sidecar`. Any failed hash or
transfer preserves the local cache.
