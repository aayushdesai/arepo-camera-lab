# Physical-channel extensions

The portable v052 scene contains cell position, density, temperature, velocity,
and stable particle ID. Rotation and outflow channels can therefore be derived
without inventing additional physics. Magnetic and thermodynamic controls use
an explicit `.fields.npz` sidecar generated from the matching HDF5 snapshot.
The join is by particle ID and all unit conversions are recorded at creation.

## Magnetic controls

With a magnetic-field vector in physical Gauss, useful exploratory channels are:

- field strength `|B|` and signed components;
- magnetic pressure `P_B = |B|^2 / (8 pi)`;
- plasma beta `beta = P_gas / P_B`;
- Alfvén speed `v_A = |B| / sqrt(4 pi rho)`;
- Alfvénic Mach number `|v| / v_A`;
- magnetic-to-kinetic pressure ratio `P_B / (rho |v|^2 / 2)`;
- field alignment with velocity, angular momentum, and the bipolar axis;
- toroidal-versus-poloidal field fraction relative to the remnant axis.

These are particularly useful for separating a magnetically collimated wind
from rotational disk material.

## Thermodynamic controls

Preferred inputs are native pressure and native entropy when the simulation
writes them. Derived alternatives must be labelled as proxies:

- gas pressure from `rho k_B T / (mu m_p)` requires an explicit mean molecular
  weight and assumes an ideal gas;
- entropy proxy `P / rho^gamma` requires an explicit adiabatic index;
- magnetic-to-gas pressure and total-pressure fractions combine the fields
  above;
- ram pressure `rho |v|^2` and outward momentum flux `rho v_out^2` need only
  density and velocity;
- sound speed and Mach number require the accepted equation of state.

For white-dwarf material, an ideal-gas pressure can be badly misleading because
degeneracy and the configured equation of state matter. The public loader should
therefore read native pressure/entropy when available or require an explicit EOS
adapter, never silently manufacture them.

## Implemented adapter contract

`arepo-camera-lab fields` accepts explicit dataset names and scale factors for
magnetic field in Gauss, gas pressure in dyn cm^-2, specific entropy in declared
cgs units, and sound speed in cm s^-1. It writes schema
`arepo_camera_lab_fields_v001`, stable particle IDs, scaled float arrays, and a
JSON provenance record into a no-clobber NPZ file. The viewer hashes the sidecar
and records its path, digest, schema, and enabled fields in the scene payload.

The adapter does not infer code units, mean molecular weight, an EOS, or
comoving conventions. Those remain the responsibility of the simulation's
verified parameter/build provenance.

For the currently audited snapshot 721, the native HDF5 output contains
`MagneticField`, `Pressure`, and `SoundSpeed`; it does not contain an entropy
dataset. Consequently the viewer can expose magnetic-field strength and
components, magnetic pressure, plasma beta, Alfvén speed, sound speed, Mach
number, and toroidal/poloidal fractions without inventing an EOS. Entropy stays
absent until an explicit native entropy dataset or reviewed EOS-derived proxy is
provided.

Both WebGL and native VTK use the same particle-ID join and the same derived
scalar definitions. Native VTK additionally exposes sampled magnetic vectors as
arrow glyphs. These glyphs show local vector direction; they are not magnetic
field-line integration.
