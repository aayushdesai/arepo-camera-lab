# AREPO Camera Lab

`arepo-camera-lab` is a local, interactive camera and physical-channel explorer
for portable AREPO full-cell scenes. It provides both a fast browser view and a
native VTK/PyVista desktop view for finding compositions before paying for a
scientific Voronoi render.

Neither point renderer is the final ray tracer. The browser uses dependency-free
WebGL and the desktop backend uses native VTK. The ArepoVTK-stellar/ArepoRT
renderer remains the scientific backend that traverses the Voronoi mesh.

## Quick start

```bash
git clone https://github.com/aayushdesai/arepo-camera-lab.git
cd arepo-camera-lab
conda env create -f environment.yml
conda activate arepo-camera-lab

# Open a deterministic synthetic disk and bipolar-outflow example.
arepo-camera-lab demo
```

To load a portable scene and keep switching files without restarting:

```bash
arepo-camera-lab serve \
  --scene /absolute/path/to/scene_v052.bin \
  --snapshot 721 \
  --max-points 400000
```

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

For a scene already stored on a local filesystem, use `--scene /local/path`.
The optional `--cache-directory` with `--scene` performs a verified local copy;
it is not needed for ordinary local files.

Immutable scenes with an already verified manifest digest can skip a second
full-file hash pass by adding `--scene-sha256 <digest>`. Files loaded from the
toolbar are hashed normally.

The server listens only on `127.0.0.1`. Scene files stay on the local computer.
The toolbar accepts another absolute scene path at any time. `400000` remains a
fast default, but there is no artificial upper point limit: enter `0` to load
every cell in the scene. The status band reports each loading phase and progress.

The display panel keeps the underlying channel values rather than baking in one
normalization. For each channel you can choose linear, log10, or symmetric-log
scaling; edit the numeric minimum and maximum; apply percentile presets; choose
from eight color maps; invert the map; and tune gamma, saturation, brightness,
point size, and opacity. The color range and units are shown beside the control.

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
- `K`: save the current camera pose for the displayed snapshot.
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
speed, B-velocity alignment, and toroidal/poloidal fractions. Pressure also
enables plasma beta; sound speed enables Mach number.

## Camera poses and spline paths

An **AREPO snapshot index** is the simulation output number: index `721` means
the data in `snapshot_0721.hdf5`. A **camera pose** is only the selected look-at
point, orientation, roll, and orthographic half extent associated with that
output. These are separate concepts throughout the UI.

The live server keeps saved poses in browser local storage while scenes are
switched. Download the combined JSON regularly. The point budget controls cell
detail only; it has no effect on the number or smoothness of spline knots.

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

Save poses from at least two epochs, then compile them against an existing
21-column `v055` timeline/template:

```bash
arepo-camera-lab spline \
  --poses stellar_camera_poses.json \
  --template accepted_camera_path.tsv \
  --output camera_spline.tsv \
  --diagnostics camera_spline_diagnostics.tsv
```

The compiler uses a cubic Hermite spline for the look-at point, quaternion
SQUAD for orientation, and a shape-preserving log-space spline for zoom. This
produces a continuously moving camera rather than a hard cut.

## Scene format

Version `0.3` reads portable `ARVTKSTARV052A` full-cell scenes containing cell
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

## VTK and exact Voronoi rendering

The WebGL and native VTK points are interactive scouting renderers. VTK gives a
more capable local desktop view, scalar pipelines, and magnetic-vector glyphs,
but cell-center points are not volume integration through Voronoi cells. A
separate progressive ArepoRT companion is planned for exact cell traversal
while the camera moves. See
[docs/voronoi-raytrace-backend.md](docs/voronoi-raytrace-backend.md) for the
boundary between the two systems.

## Development

```bash
python -m unittest discover -s tests -v
```

The project uses no-clobber output behavior for generated scenes, HTML files,
camera paths, and diagnostics.
