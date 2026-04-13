# TATTOOS – Water Flow Network Simulator

An interactive GUI for order-of-magnitude water-flow calculations in pipe
networks: pumps, valves, header tanks, parallel branches, and user-defined
pressure-drop devices.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# On Debian/Ubuntu you also need:
sudo apt-get install python3-tk

# 2. Run
python water_flow_sim.py
```

---

## Features

| Feature | Description |
|---|---|
| **Pump** | Variable pressure (bar) and max-flow (L/min) |
| **Header tank** | Static head above pump adds ρ·g·h pressure |
| **Pipes** | Diameter (mm) + length (m); Hagen-Poiseuille resistance |
| **Valves** | 0–100 % opening; modelled as 1 cm pipe with scaled diameter |
| **Devices** | User-named pressure-drop element (ΔP @ reference flow) |
| **Parallel branches** | Add/remove branches; all see the same source pressure |
| **Series components** | Components within a branch are in series |
| **Live results** | Flow rate (L/min), ΔP (bar/mbar), velocity (m/s), Reynolds No. |

---

## Physics (simplified – order of magnitude)

* **Pipe resistance** (laminar Hagen-Poiseuille):  
  `R = 128 · μ · L / (π · d⁴)` &emsp; [Pa·s/m³]

* **Pressure drop**:  
  `ΔP = R · Q`

* **Valve model** (1 cm section with variable diameter):  
  `d_eff = d_pipe · √(open_fraction)`

* **Header-tank static pressure**:  
  `P_tank = ρ · g · h`

* Parallel branches share the same ΔP; flows are summed.
* Series components share the same Q; pressure drops are summed.
* Pump max-flow clamps total flow (simplified pump curve).

> Note: turbulent corrections and minor-loss coefficients are intentionally
> omitted to keep the simulation simple and fast.

---

## GUI Controls

* **Toolbar spinboxes** – adjust pump pressure / max-flow, header-tank height.
* **+ Pipe / + Valve / + Device** – append a component to the selected branch.
* **+ Branch / – Branch** – add or remove a parallel branch.
* **Branch list (right panel)** – click a branch name to select it.
* **Canvas** – click a component to select it; its properties appear on the right.
* **Properties panel** – drag sliders or type values, then press *Apply Changes*.
* **▶ Run Simulation** – solve the network and populate the results table.
* **🗑 Delete** – remove the currently selected component.
