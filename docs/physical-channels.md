# Physical-channel extensions

The portable v052 scene currently contains cell position, density, temperature,
velocity, and stable particle ID. Rotation and outflow channels can therefore
be derived without inventing additional physics. Magnetic and thermodynamic
controls require an extended scene contract or an explicit raw-snapshot field
map.

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

## Proposed adapter contract

A raw-HDF5 adapter should accept a checked mapping file that specifies dataset
names, unit conversions, comoving/physical state, magnetic-unit convention,
mean molecular weight or EOS provenance, and optional composition fields. The
resulting portable scene should record every enabled channel and formula in its
manifest so browser colors remain reproducible.
