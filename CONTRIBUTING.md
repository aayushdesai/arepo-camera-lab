# Developing AREPO Camera Lab

Document while building. Update the relevant documentation as each behavior,
design decision, or verification result becomes clear, and commit it with the
implementation it describes. Keep notes useful throughout the work, including
while a feature is incomplete.

| Change | Documentation to update alongside it |
| --- | --- |
| Controls, commands, setup, or supported inputs | [README](README.md) and the relevant example under `examples/` |
| Mesh geometry, rendering, units, measurements, or implementation tradeoffs | [Native viewer notes](docs/voronoi-raytrace-backend.md) or the relevant focused document under `docs/` |
| Capture or verification results | The feature's verification notes and the output's configuration, manifest, and image captions |

Explain what changed, why it works that way, and how to use or reproduce it.
Keep implemented behavior, verified behavior, and planned work explicit. Record
actual checks and their scope; retain failures or blocked checks until resolved.
When reporting performance, state the hardware, workload, and excluded costs.
When implementation changes direction, revise the affected explanation and
examples in the same step so they describe the code that exists.

Preserve scientific provenance: units, coordinate conventions, snapshot/time
bindings, source hashes, and distinctions between geometry previews and optical
rendering belong beside the feature. Keep generated data, large images, and
machine-specific paths in their existing no-clobber output directories, with
portable usage examples in the repository.
