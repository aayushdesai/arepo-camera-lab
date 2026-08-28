# Progressive Voronoi ray-tracing backend

The WebGL point renderer and an exact Voronoi renderer solve different
problems. They should share camera poses and transfer definitions, but remain
separate execution backends.

## Interactive point modes

The browser and native VTK modes sample deterministic cell centers and draw
them as colored points. Native VTK also supports scalar clipping and magnetic
vector glyphs. They are suitable for:

- choosing a view and look-at point;
- exploring density, temperature, kinematics, and geometry;
- authoring camera poses across simulation snapshots;
- evaluating whether a camera motion is worth an exact render.

Neither mode integrates opacity through Voronoi cells and therefore neither can
be used as the final scientific image.

## Exact progressive mode

The exact mode should run as a local native/CUDA companion process:

1. Load full cell records, face topology, and transfer-profile constants once.
2. Keep immutable scene and neighbor data resident on the GPU.
3. Accept camera pose, image size, and transfer parameters over a loopback API.
4. Generate orthographic rays for the current camera and traverse native
   Voronoi faces rather than splatting points.
5. While the user drags, render a coarse image such as 320x180 with fewer
   samples; after a short idle period, refine to 640x360 or 960x540.
6. Return pixels plus validity/status counts and deterministic provenance.

The browser or VTK window remains responsible for controls and pose capture.
The companion owns mesh traversal, CPU/CUDA resources, and scientific validity.

## Acceptance boundary

Before this backend is advertised as exact, it needs:

- the same inactive-ray and fatal-status semantics as the validated native
  renderer;
- double-precision cell geometry where required;
- native display encoding and transfer-profile parity;
- deterministic repeated output on each supported GPU family;
- frozen image/status comparisons against the native Voronoi renderer;
- explicit marking of preview-only output until those gates pass.

This architecture avoids forcing millions of topology records through browser
JavaScript while still giving the user interactive camera feedback.
