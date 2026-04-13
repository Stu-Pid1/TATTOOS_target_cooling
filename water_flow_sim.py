#!/usr/bin/env python3
"""
TATTOOS - Water Flow Network Simulator

Interactive GUI for order-of-magnitude water flow calculations
in pipe networks with pumps, valves, header tanks, and devices.

Physics model (simplified laminar flow):
  - Pipe resistance (Hagen-Poiseuille): R = 128·μ·L / (π·d⁴)  [Pa·s/m³]
  - Pressure drop: ΔP = R · Q
  - Valve: modelled as a 1 cm pipe section where
           d_effective = d_pipe · √(open_fraction)
  - Header tank: P_static = ρ·g·h
  - Parallel branches: same ΔP, flows add
  - Series components: same Q, pressures add

Usage:
    python water_flow_sim.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import math

# ─── Physics constants ──────────────────────────────────────────────────────
RHO        = 1000.0   # water density                [kg/m³]
MU         = 0.001    # dynamic viscosity            [Pa·s]  (water @ ~20 °C)
G_ACC      = 9.81     # gravitational accel.         [m/s²]
CP_WATER   = 4186.0   # water specific heat          [J/(kg·K)]
CP_GLYCOL  = 3500.0   # ~50 % EG mixture spec. heat  [J/(kg·K)]
RHO_GLYCOL = 1060.0   # ~50 % EG mixture density     [kg/m³]

# ─── Unit helpers ───────────────────────────────────────────────────────────
def lpm_to_m3s(q):  return q / 60_000.0
def m3s_to_lpm(q):  return q * 60_000.0
def bar_to_pa(p):   return p * 1e5
def pa_to_bar(p):   return p / 1e5

# ─── Hydraulic functions ────────────────────────────────────────────────────
def pipe_R(d_m, l_m):
    """Hagen-Poiseuille resistance [Pa·s/m³]:  ΔP = R · Q"""
    if d_m < 1e-9 or l_m <= 0:
        return 1e20
    return 128.0 * MU * l_m / (math.pi * d_m ** 4)


def valve_R(d_m, open_frac):
    """
    Valve modelled as a 1 cm pipe section with
        d_eff = d_pipe · √(open_fraction)
    (per specification: simulate 1 cm section that is 0→1 of the pipe)
    """
    if open_frac < 1e-4:
        return 1e20          # fully closed
    d_eff = d_m * math.sqrt(max(open_frac, 1e-5))
    return pipe_R(d_eff, 0.01)


def pipe_velocity(q_m3s, d_m):
    """Mean flow velocity [m/s]"""
    if d_m < 1e-9:
        return 0.0
    return abs(q_m3s) / (math.pi * (d_m / 2) ** 2)


def reynolds_number(q_m3s, d_m):
    v = pipe_velocity(q_m3s, d_m)
    return RHO * v * d_m / MU


def hx_effectiveness(UA, C_hot, C_cold):
    """
    Counter-flow heat-exchanger NTU-effectiveness.
    Returns ε ∈ [0, 1].
    """
    C_min = min(C_hot, C_cold)
    C_max = max(C_hot, C_cold)
    if C_min < 1e-9 or UA <= 0 or C_max < 1e-9:
        return 0.0
    NTU = UA / C_min
    C_r = C_min / C_max
    if abs(1.0 - C_r) < 1e-6:           # balanced streams
        return NTU / (1.0 + NTU)
    exp_t = math.exp(-NTU * (1.0 - C_r))
    return min((1.0 - exp_t) / (1.0 - C_r * exp_t), 1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  COMPONENT CLASSES
# ═══════════════════════════════════════════════════════════════════════════

class BaseComp:
    """Base class for all hydraulic network components."""
    _ctr = 0
    COLOR = '#95A5A6'

    def __init__(self, kind, label):
        BaseComp._ctr += 1
        self.uid   = BaseComp._ctr
        self.kind  = kind
        self.label = label
        # Filled by solver after each simulation run
        self.q_m3s = 0.0   # flow rate      [m³/s]
        self.dp_pa = 0.0   # pressure drop  [Pa]
        self.T_in  = 0.0   # water inlet T  [°C]
        self.T_out = 0.0   # water outlet T [°C]

    def resistance(self):
        """Hydraulic resistance [Pa·s/m³]"""
        return 0.0

    def canvas_label(self):
        return self.label

    def result_str(self):
        q_lpm  = m3s_to_lpm(self.q_m3s)
        dp_pa  = self.dp_pa
        dp_bar = pa_to_bar(self.dp_pa)
        return (f"Q = {q_lpm:.3f} L/min\n"
                f"ΔP = {dp_pa:.2f} Pa  ({dp_bar*1000:.3f} mbar)")

    def velocity(self):
        """Mean flow velocity [m/s] — overridden by components with a bore."""
        return 0.0

    def reynolds(self):
        """Reynolds number — overridden by components with a bore."""
        return 0.0


class PumpComp(BaseComp):
    COLOR = '#C0392B'

    def __init__(self):
        super().__init__('pump', 'Pump')
        self.pbar = 1.0    # nominal pressure  [bar]
        self.qmax = 20.0   # max flow rate     [L/min]

    def source_pa(self):
        return bar_to_pa(self.pbar)

    def canvas_label(self):
        return f"PUMP\n{self.pbar:.1f} bar\nmax {self.qmax:.0f} L/min"


class TankComp(BaseComp):
    COLOR = '#8E44AD'

    def __init__(self):
        super().__init__('tank', 'Header Tank')
        self.h_m  = 2.0    # height above pump [m]
        self.vol  = 50.0   # tank volume       [L]

    def source_pa(self):
        return RHO * G_ACC * self.h_m

    def canvas_label(self):
        return f"TANK\n{self.h_m:.1f} m\n({self.source_pa():.0f} Pa)"


class HeatExchangerComp(BaseComp):
    """
    Liquid-to-liquid heat exchanger on the main water circuit line.

    Water side  : pump outlet → HX → manifold (cools the return water).
    Glycol side : user-defined glycol flow and inlet temperature.

    Hydraulic   : fixed ΔP on the water side (dp_pa).
    Thermal     : counter-flow NTU-effectiveness model.
    """
    COLOR = '#117A65'

    def __init__(self):
        super().__init__('hx', 'Heat Exchanger')
        self.T_glycol_in  = 15.0    # glycol inlet temperature    [°C]
        self.Q_glycol_lpm = 8.0     # glycol volumetric flow rate [L/min]
        self.UA           = 300.0   # overall UA coefficient      [W/K]
        self.dp_pa        = 200.0   # water-side pressure drop    [Pa]
        # Results – populated by thermal solver
        self.T_water_in   = 0.0     # = T_return                  [°C]
        self.T_water_out  = 0.0     # = T_manifold                [°C]
        self.T_glycol_out = 0.0     # computed glycol outlet T    [°C]
        self.Q_duty       = 0.0     # heat removed from water     [W]

    def canvas_label(self):
        return (f"HX\nUA={self.UA:.0f} W/K\n"
                f"Glycol {self.Q_glycol_lpm:.1f} L/min @ {self.T_glycol_in:.0f}°C")


class PipeComp(BaseComp):
    COLOR = '#2980B9'

    def __init__(self, d_mm=15.0, l_m=1.0):
        super().__init__('pipe', 'Pipe')
        self.d_mm = float(d_mm)
        self.l_m  = float(l_m)

    def resistance(self):
        return pipe_R(self.d_mm / 1000.0, self.l_m)

    def velocity(self):
        return pipe_velocity(self.q_m3s, self.d_mm / 1000.0)

    def reynolds(self):
        return reynolds_number(self.q_m3s, self.d_mm / 1000.0)

    def canvas_label(self):
        return f"PIPE\nØ{self.d_mm:.0f} mm\nL={self.l_m:.1f} m"


class ValveComp(BaseComp):
    COLOR = '#27AE60'

    def __init__(self, d_mm=15.0, pct=100.0):
        super().__init__('valve', 'Valve')
        self.d_mm = float(d_mm)
        self.pct  = float(pct)   # 0–100 %

    def resistance(self):
        return valve_R(self.d_mm / 1000.0, self.pct / 100.0)

    def velocity(self):
        return pipe_velocity(self.q_m3s, self.d_mm / 1000.0)

    def reynolds(self):
        return reynolds_number(self.q_m3s, self.d_mm / 1000.0)

    def canvas_label(self):
        return f"VALVE\n{self.pct:.0f}% open\nØ{self.d_mm:.0f} mm"


class DeviceComp(BaseComp):
    COLOR = '#D35400'

    def __init__(self, name="Device", dp_pa=5000.0, qref_lpm=5.0):
        super().__init__('device', name)
        self.dname    = name
        self.dp_ref   = float(dp_pa)     # Pa at reference flow
        self.qref_lpm = float(qref_lpm)  # reference flow [L/min]

    def resistance(self):
        qr = lpm_to_m3s(self.qref_lpm)
        if qr < 1e-12:
            return 0.0
        return self.dp_ref / qr

    def canvas_label(self):
        return f"{self.dname}\nΔP={self.dp_ref:.0f} Pa\n@{self.qref_lpm:.1f} L/min"


class HeatSourceComp(BaseComp):
    """
    A thermal load on the water circuit — adds Q_W watts of heat to the water
    and also presents a hydraulic resistance (linear ΔP model).

    Represents targets, cold-plates, or any powered device being cooled.
    """
    COLOR = '#E74C3C'

    def __init__(self, name="Heat Source", Q_W=100.0, dp_pa=500.0, qref_lpm=5.0):
        super().__init__('heatsource', name)
        self.hname    = name
        self.Q_W      = float(Q_W)       # heat added to water  [W]
        self.dp_ref   = float(dp_pa)     # Pa at reference flow
        self.qref_lpm = float(qref_lpm)  # reference flow       [L/min]

    def resistance(self):
        qr = lpm_to_m3s(self.qref_lpm)
        return self.dp_ref / qr if qr > 1e-12 else 0.0

    def canvas_label(self):
        return f"{self.hname}\nQ={self.Q_W:.0f} W\nΔP={self.dp_ref:.0f} Pa"


# ═══════════════════════════════════════════════════════════════════════════
#  NETWORK MODEL
# ═══════════════════════════════════════════════════════════════════════════

class Branch:
    """A series sequence of passive components forming one parallel path."""
    _ctr = 0

    def __init__(self):
        Branch._ctr += 1
        self.bid   = Branch._ctr
        self.name  = f"Branch {self.bid}"
        self.comps = []   # list[BaseComp]

    def total_R(self):
        """Sum of component resistances (series path)."""
        return sum(c.resistance() for c in self.comps)

    def apply_flow(self, q_m3s):
        """Distribute flow and compute pressure drop for each component."""
        for c in self.comps:
            c.q_m3s = q_m3s
            c.dp_pa = q_m3s * c.resistance()


class HydroNetwork:
    """
    Parallel-branch hydraulic network.

    Topology:
        PUMP (+ optional TANK) → [Branch 1 | Branch 2 | …] → RETURN

    All branches are in parallel, so each sees the same source pressure.
    Branch flow = P_source / R_branch.
    Total flow = Σ branch flows, clamped to pump Q_max.
    """

    def __init__(self):
        self.pump      = PumpComp()
        self.tank      = TankComp()
        self.use_tank  = False
        self.hx        = HeatExchangerComp()
        self.use_hx    = False
        self.branches  = [Branch()]
        # Thermal results – populated by simulate()
        self.T_manifold   = None   # [°C] water temp at manifold inlet
        self.T_return     = None   # [°C] water temp returning to HX/pump
        self.Q_total_heat = 0.0    # [W]  total heat from all heat sources

    def add_branch(self):
        b = Branch()
        self.branches.append(b)
        return b

    def remove_branch(self, branch):
        if len(self.branches) > 1:
            self.branches.remove(branch)

    def source_pa(self):
        p = self.pump.source_pa()
        if self.use_tank:
            p += self.tank.source_pa()
        if self.use_hx:
            p = max(0.0, p - self.hx.dp_pa)
        return p

    def simulate(self):
        """
        Solve the network and populate .q_m3s / .dp_pa on every component.
        Then run the steady-state thermal solver.
        Returns a results dict for display.
        """
        # ── Hydraulic solve ──────────────────────────────────────────────────
        P    = self.source_pa()
        Qmax = lpm_to_m3s(self.pump.qmax)

        R_list = [b.total_R() for b in self.branches]

        # Ideal flow in each branch
        Q_list = []
        for R in R_list:
            if R > 1e18:
                Q_list.append(0.0)
            elif R < 1e-15:
                Q_list.append(Qmax)
            else:
                Q_list.append(P / R)

        Q_total = sum(Q_list)

        # Clamp to pump max flow (pump curve simplification)
        if Q_total > Qmax and Q_total > 0:
            scale   = Qmax / Q_total
            Q_list  = [q * scale for q in Q_list]
            Q_total = Qmax
            # Recompute effective operating pressure
            G_total = sum(1.0 / R for R in R_list
                          if 1e-15 < R < 1e18)
            P_actual = Q_total / G_total if G_total > 0 else P
        else:
            P_actual = P

        # Apply flows to branches
        for b, q in zip(self.branches, Q_list):
            b.apply_flow(q)

        # Pump / tank results
        self.pump.q_m3s = Q_total
        self.pump.dp_pa = P_actual
        if self.use_tank:
            self.tank.q_m3s = Q_total
            self.tank.dp_pa = self.tank.source_pa()

        # ── Steady-state thermal solve ────────────────────────────────────────
        self._thermal_solve(Q_total, Q_list)

        return {
            'P_source':  P,
            'P_actual':  P_actual,
            'Q_total':   Q_total,
            'Q_list':    Q_list,
            'R_list':    R_list,
        }

    def _thermal_solve(self, Q_total_m3s, Q_list):
        """
        Steady-state temperature calculation.

        Energy balance for the closed loop (zero heat capacity in reservoir):
          Q_removed_by_HX  =  Q_total_heat_sources

        Using the NTU-effectiveness method for a counter-flow HX:
          T_return  = T_glycol_in + Q_heat / (ε · C_min)
          T_manifold = T_return - Q_heat / C_water

        Per branch:
          ΔT_component = Q_source_W / (ṁ_branch · cp_water)
        """
        # Total heat from all heat sources [W]
        Q_heat = sum(
            c.Q_W
            for b in self.branches
            for c in b.comps
            if isinstance(c, HeatSourceComp)
        )
        self.Q_total_heat = Q_heat

        # Water capacity rate [W/K]
        m_dot_water = Q_total_m3s * RHO          # kg/s
        C_water     = m_dot_water * CP_WATER      # W/K

        if self.use_hx and C_water > 1e-9:
            m_dot_gly = lpm_to_m3s(self.hx.Q_glycol_lpm) * RHO_GLYCOL
            C_glycol  = m_dot_gly * CP_GLYCOL     # W/K
            C_min     = min(C_water, C_glycol)
            C_max     = max(C_water, C_glycol)
            eps       = hx_effectiveness(self.hx.UA, C_min, C_max)

            if eps * C_min > 1e-9 and Q_heat > 0:
                T_return    = self.hx.T_glycol_in + Q_heat / (eps * C_min)
                T_manifold  = T_return - Q_heat / C_water
                T_gly_out   = (self.hx.T_glycol_in + Q_heat / C_glycol
                               if C_glycol > 1e-9
                               else self.hx.T_glycol_in)
            else:
                # No heat sources → isothermal at glycol inlet temp
                T_return   = self.hx.T_glycol_in
                T_manifold = self.hx.T_glycol_in
                T_gly_out  = self.hx.T_glycol_in

            # Store HX results
            self.hx.T_water_in   = T_return
            self.hx.T_water_out  = T_manifold
            self.hx.T_glycol_out = T_gly_out
            self.hx.Q_duty       = Q_heat
        else:
            # No HX — use 20 °C reference (ambient)
            T_manifold = 20.0
            T_return   = 20.0 + (Q_heat / C_water if C_water > 1e-9 else 0.0)

        self.T_manifold = T_manifold
        self.T_return   = T_return

        # ── Per-branch per-component temperatures ────────────────────────────
        for b, q_branch in zip(self.branches, Q_list):
            m_dot_b = q_branch * RHO              # kg/s
            C_b     = m_dot_b * CP_WATER          # W/K
            T_cur   = T_manifold
            for comp in b.comps:
                comp.T_in = T_cur
                if isinstance(comp, HeatSourceComp) and C_b > 1e-9:
                    T_cur += comp.Q_W / C_b
                comp.T_out = T_cur


# ═══════════════════════════════════════════════════════════════════════════
#  GUI APPLICATION
# ═══════════════════════════════════════════════════════════════════════════

# Canvas geometry constants
COMP_W  = 110   # component box width  (px)
COMP_H  = 60    # component box height (px)
COMP_GAP = 20   # horizontal gap between boxes (wider = easier to click)
ROW_H   = 100   # vertical space per branch row
PUMP_W  = 100
PUMP_H  = 80
HX_W    = 120   # heat exchanger box width (px)
HX_GAP  = 20    # gap between pump / HX / manifold
LEFT_PAD  = 20
RIGHT_PAD = 20
TOP_PAD   = 30
BRANCH_START_X = LEFT_PAD + PUMP_W + 30   # x where branch components begin (no HX)
# When HX is present, branch start is pushed right by HX_W + HX_GAP*2
HX_BRANCH_START_X = LEFT_PAD + PUMP_W + HX_GAP + HX_W + HX_GAP

# Colours for flow-rate heat-map on canvas (low→high)
FLOW_COLORS = ['#AED6F1', '#5DADE2', '#2E86C1', '#1A5276']


def flow_color(q_lpm, q_max_lpm=20.0):
    """Map a flow rate to a heat-map colour."""
    if q_max_lpm <= 0:
        return FLOW_COLORS[0]
    idx = int(min(q_lpm / q_max_lpm, 1.0) * (len(FLOW_COLORS) - 1))
    return FLOW_COLORS[idx]


class PropertiesPanel(tk.Frame):
    """Right-side panel showing editable properties for the selected component."""

    def __init__(self, parent, on_apply):
        super().__init__(parent, bd=1, relief='sunken', bg='#ECF0F1')
        self.on_apply = on_apply
        self._comp = None
        self._vars = {}

        tk.Label(self, text="Properties", font=('Arial', 11, 'bold'),
                 bg='#2C3E50', fg='white').pack(fill='x')

        self.inner = tk.Frame(self, bg='#ECF0F1')
        self.inner.pack(fill='both', expand=True, padx=6, pady=6)

        self.result_lbl = tk.Label(self, text="", bg='#ECF0F1',
                                   justify='left', font=('Courier', 9))
        self.result_lbl.pack(fill='x', padx=6, pady=(0, 4))

        tk.Button(self, text="Apply Changes", command=self._apply,
                  bg='#27AE60', fg='white', relief='flat').pack(
                  fill='x', padx=6, pady=4)

    # ── public API ──────────────────────────────────────────────────────────

    def load(self, comp):
        self._comp = comp
        self._vars.clear()
        for w in self.inner.winfo_children():
            w.destroy()

        if comp is None:
            tk.Label(self.inner,
                     text="Click a component or line\non the canvas to interact.",
                     bg='#ECF0F1', fg='#7F8C8D', justify='center').pack(pady=20)
            self.result_lbl.config(text="")
            return

        fields = self._fields_for(comp)
        for row, (fname, val, unit, lo, hi, ftype) in enumerate(fields):
            tk.Label(self.inner, text=fname, bg='#ECF0F1',
                     anchor='w').grid(row=row, column=0, sticky='w', pady=2)
            if ftype == 'scale' and lo is not None and hi is not None:
                var = tk.DoubleVar(value=val)
                frame = tk.Frame(self.inner, bg='#ECF0F1')
                frame.grid(row=row, column=1, columnspan=2, sticky='ew', pady=2)
                lbl = tk.Label(frame, text=f"{val:.1f}", bg='#ECF0F1', width=6)
                lbl.pack(side='right')
                sc = tk.Scale(frame, variable=var, from_=lo, to=hi,
                              orient='horizontal', resolution=(hi - lo) / 200,
                              bg='#ECF0F1', showvalue=False, length=130,
                              command=lambda v, l=lbl, u=unit: l.config(
                                  text=f"{float(v):.1f}{u}"))
                sc.pack(side='left', fill='x', expand=True)
            elif ftype == 'entry':
                var = tk.StringVar(value=str(val))
                e = tk.Entry(self.inner, textvariable=var, width=12)
                e.grid(row=row, column=1, sticky='ew', pady=2)
                tk.Label(self.inner, text=unit, bg='#ECF0F1').grid(
                    row=row, column=2, sticky='w')
            else:
                var = tk.DoubleVar(value=val)
                e = tk.Entry(self.inner, textvariable=var, width=10)
                e.grid(row=row, column=1, sticky='ew', pady=2)
                tk.Label(self.inner, text=unit, bg='#ECF0F1').grid(
                    row=row, column=2, sticky='w')
            self._vars[fname] = (var, ftype)

        self.inner.columnconfigure(1, weight=1)
        self.result_lbl.config(text=comp.result_str())

    def refresh_results(self, comp):
        if comp is self._comp and comp is not None:
            self.result_lbl.config(text=comp.result_str())

    def load_line_info(self, branch, insert_idx, on_insert):
        """Show the insert-here panel when a line segment is selected."""
        self._comp = None
        self._vars.clear()
        for w in self.inner.winfo_children():
            w.destroy()

        # Describe the insertion position
        n = len(branch.comps)
        if n == 0:
            pos_text = f"Start of {branch.name} (empty)"
        elif insert_idx == 0:
            first = branch.comps[0].canvas_label().splitlines()[0]
            pos_text = f"Before  '{first}'"
        elif insert_idx >= n:
            last = branch.comps[-1].canvas_label().splitlines()[0]
            pos_text = f"After  '{last}'"
        else:
            a = branch.comps[insert_idx - 1].canvas_label().splitlines()[0]
            b = branch.comps[insert_idx].canvas_label().splitlines()[0]
            pos_text = f"Between '{a}'\nand '{b}'"

        tk.Label(self.inner, text=f"✦ {branch.name}",
                 bg='#ECF0F1', fg='#2C3E50',
                 font=('Arial', 9, 'bold'), anchor='w').pack(fill='x', pady=(4, 0))
        tk.Label(self.inner, text=pos_text,
                 bg='#ECF0F1', fg='#5D6D7E',
                 font=('Arial', 8), wraplength=190,
                 justify='left', anchor='w').pack(fill='x', pady=(0, 8))

        sep = tk.Frame(self.inner, bg='#BDC3C7', height=1)
        sep.pack(fill='x', pady=(0, 6))

        tk.Label(self.inner, text="Insert here:",
                 bg='#ECF0F1', font=('Arial', 9, 'bold'), anchor='w').pack(fill='x')

        btn_f = tk.Frame(self.inner, bg='#ECF0F1')
        btn_f.pack(fill='x', pady=4)
        bs = {'relief': 'flat', 'pady': 4, 'padx': 6, 'bd': 0, 'font': ('Arial', 8)}
        tk.Button(btn_f, text="+ Pipe",        bg='#2980B9', fg='white',
                  command=lambda: on_insert('pipe'),        **bs).pack(side='left', padx=2)
        tk.Button(btn_f, text="+ Valve",       bg='#27AE60', fg='white',
                  command=lambda: on_insert('valve'),       **bs).pack(side='left', padx=2)
        tk.Button(btn_f, text="+ Heat Source", bg='#E74C3C', fg='white',
                  command=lambda: on_insert('heatsource'),  **bs).pack(side='left', padx=2)

        self.result_lbl.config(text="")

    # ── internals ───────────────────────────────────────────────────────────

    def _apply(self):
        if self._comp is None:
            return
        c = self._comp
        try:
            vals = {k: (float(v.get()) if ft != 'entry' else v.get())
                    for k, (v, ft) in self._vars.items()}
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return
        self._write_back(c, vals)
        self.on_apply()

    @staticmethod
    def _fields_for(comp):
        """Return list of (field_name, value, unit, lo, hi, ftype)."""
        if isinstance(comp, PumpComp):
            return [
                ('Pressure',  comp.pbar, 'bar',   0.1,  20.0, 'scale'),
                ('Max Flow',  comp.qmax, 'L/min',  1.0, 200.0, 'scale'),
            ]
        if isinstance(comp, TankComp):
            return [
                ('Height',  comp.h_m, 'm',  0.1, 30.0, 'scale'),
                ('Volume',  comp.vol, 'L',  1.0, 500.0, 'scale'),
            ]
        if isinstance(comp, HeatExchangerComp):
            return [
                ('Glycol T_in',  comp.T_glycol_in,  '°C',   -20.0, 80.0,  'scale'),
                ('Glycol flow',  comp.Q_glycol_lpm, 'L/min', 0.1,  50.0,  'scale'),
                ('UA',           comp.UA,            'W/K',   10.0, 5000.0,'scale'),
                ('Water-side ΔP',comp.dp_pa,         'Pa',    0.0,  5000.0,'scale'),
            ]
        if isinstance(comp, PipeComp):
            return [
                ('Diameter', comp.d_mm, 'mm', 1.0,  100.0, 'scale'),
                ('Length',   comp.l_m,  'm',  0.01, 200.0, 'scale'),
            ]
        if isinstance(comp, ValveComp):
            return [
                ('Diameter', comp.d_mm, 'mm',  1.0, 100.0, 'scale'),
                ('Opening',  comp.pct,  '%',   0.0, 100.0, 'scale'),
            ]
        if isinstance(comp, HeatSourceComp):
            return [
                ('Name',     comp.hname,    '',       None, None,   'entry'),
                ('Heat Q',   comp.Q_W,      'W',      0.0,  5000.0, 'scale'),
                ('ΔP ref',   comp.dp_ref,   'Pa',     0.0,  1e6,    'scale'),
                ('Q ref',    comp.qref_lpm, 'L/min',  0.01, 100.0,  'scale'),
            ]
        if isinstance(comp, DeviceComp):
            return [
                ('Name',     comp.dname,    '',      None, None,  'entry'),
                ('ΔP ref',   comp.dp_ref,   'Pa',    0.0,  1e6,   'scale'),
                ('Q ref',    comp.qref_lpm, 'L/min', 0.01, 100.0, 'scale'),
            ]
        return []

    @staticmethod
    def _write_back(comp, vals):
        if isinstance(comp, PumpComp):
            comp.pbar = vals['Pressure']
            comp.qmax = vals['Max Flow']
        elif isinstance(comp, TankComp):
            comp.h_m  = vals['Height']
            comp.vol  = vals['Volume']
        elif isinstance(comp, HeatExchangerComp):
            comp.T_glycol_in  = vals['Glycol T_in']
            comp.Q_glycol_lpm = vals['Glycol flow']
            comp.UA           = vals['UA']
            comp.dp_pa        = vals['Water-side ΔP']
        elif isinstance(comp, PipeComp):
            comp.d_mm = vals['Diameter']
            comp.l_m  = vals['Length']
        elif isinstance(comp, ValveComp):
            comp.d_mm = vals['Diameter']
            comp.pct  = vals['Opening']
        elif isinstance(comp, HeatSourceComp):
            comp.hname    = vals['Name']
            comp.label    = vals['Name']
            comp.Q_W      = vals['Heat Q']
            comp.dp_ref   = vals['ΔP ref']
            comp.qref_lpm = vals['Q ref']
        elif isinstance(comp, DeviceComp):
            comp.dname    = vals['Name']
            comp.label    = vals['Name']
            comp.dp_ref   = vals['ΔP ref']
            comp.qref_lpm = vals['Q ref']


class ResultsPanel(tk.Frame):
    """Bottom panel showing a table of simulation results."""

    COLS = ('Component', 'Branch', 'Flow (L/min)', 'ΔP (Pa)', 'ΔP (mbar)', 'ΔP (bar)',
            'Velocity (m/s)', 'Reynolds No.', 'T_out (°C)', 'Q_heat (W)')

    def __init__(self, parent):
        super().__init__(parent, bd=1, relief='sunken')
        tk.Label(self, text="Simulation Results",
                 font=('Arial', 10, 'bold'), bg='#2C3E50', fg='white').pack(fill='x')

        frame = tk.Frame(self)
        frame.pack(fill='both', expand=True)

        vsb = ttk.Scrollbar(frame, orient='vertical')
        vsb.pack(side='right', fill='y')
        hsb = ttk.Scrollbar(frame, orient='horizontal')
        hsb.pack(side='bottom', fill='x')

        self.tree = ttk.Treeview(frame, columns=self.COLS, show='headings',
                                 height=6, yscrollcommand=vsb.set,
                                 xscrollcommand=hsb.set)
        vsb.config(command=self.tree.yview)
        hsb.config(command=self.tree.xview)

        widths = [160, 80, 100, 80, 90, 80, 110, 100, 90, 90]
        for col, w in zip(self.COLS, widths):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, anchor='center', minwidth=60)
        self.tree.pack(fill='both', expand=True)

        self.summary_var = tk.StringVar(value="Run simulation to see results.")
        tk.Label(self, textvariable=self.summary_var, anchor='w',
                 font=('Arial', 9), bg='#F0F0F0').pack(fill='x', padx=4)

    def update(self, network, results):
        """Populate the table with latest simulation data."""
        for row in self.tree.get_children():
            self.tree.delete(row)

        # Pump row
        q  = m3s_to_lpm(network.pump.q_m3s)
        dp = pa_to_bar(network.pump.dp_pa)
        self.tree.insert('', 'end', values=(
            'Pump', '—',
            f"{q:.3f}", f"{dp*1e5:.1f}", f"{dp*1000:.2f}", f"{dp:.4f}", '—', '—', '—', '—'
        ))

        # Tank row (if used)
        if network.use_tank:
            dp_t = pa_to_bar(network.tank.dp_pa)
            self.tree.insert('', 'end', values=(
                'Header Tank', '—', f"{q:.3f}",
                f"{dp_t*1e5:.1f}", f"{dp_t*1000:.2f}", f"{dp_t:.4f}", '—', '—', '—', '—'
            ))

        # HX row (if used)
        if network.use_hx and network.T_manifold is not None:
            hx = network.hx
            dp_hx = pa_to_bar(hx.dp_pa)
            self.tree.insert('', 'end', values=(
                f"HX | UA={hx.UA:.0f} W/K | Glycol {hx.Q_glycol_lpm:.1f} L/min",
                '—', f"{q:.3f}",
                f"{hx.dp_pa:.1f}", f"{dp_hx*1000:.2f}", f"{dp_hx:.5f}",
                '—', '—',
                f"{hx.T_water_out:.1f}",
                f"−{hx.Q_duty:.0f}"   # negative = heat removed
            ))

        # Component rows
        for b in network.branches:
            for comp in b.comps:
                ql    = m3s_to_lpm(comp.q_m3s)
                dp_pa = comp.dp_pa
                dp_b  = pa_to_bar(dp_pa)
                vel   = comp.velocity()
                re    = comp.reynolds()
                t_out = f"{comp.T_out:.1f}" if comp.T_out > 0.0 else '—'
                q_heat = (f"{comp.Q_W:.0f}" if isinstance(comp, HeatSourceComp)
                          else '—')
                self.tree.insert('', 'end', values=(
                    comp.canvas_label().replace('\n', ' | '),
                    b.name,
                    f"{ql:.3f}",
                    f"{dp_pa:.2f}",
                    f"{dp_b*1000:.3f}",
                    f"{dp_b:.5f}",
                    f"{vel:.3f}" if vel > 0 else '—',
                    f"{re:.0f}" if re > 0 else '—',
                    t_out,
                    q_heat,
                ))

        qtot = m3s_to_lpm(results['Q_total'])
        thermal_str = ""
        if network.T_manifold is not None:
            thermal_str = (f"  |  T_manifold: {network.T_manifold:.1f} °C"
                           f"  |  T_return: {network.T_return:.1f} °C"
                           f"  |  Total heat: {network.Q_total_heat:.0f} W")
            if network.use_hx:
                thermal_str += (f"  |  Glycol out: {network.hx.T_glycol_out:.1f} °C"
                                f"  |  HX duty: {network.hx.Q_duty:.0f} W")
        self.summary_var.set(
            f"Total flow: {qtot:.2f} L/min  |  "
            f"Source pressure: {pa_to_bar(results['P_source']):.3f} bar  |  "
            f"Operating pressure: {pa_to_bar(results['P_actual']):.3f} bar"
            + thermal_str
        )


class CanvasView(tk.Frame):
    """Central canvas showing the schematic of the hydraulic network."""

    def __init__(self, parent, on_select, on_line_select=None):
        super().__init__(parent)
        self.on_select      = on_select       # callback(comp_or_None)
        self.on_line_select = on_line_select or (lambda b, i: None)
        self._net      = None
        self._sel      = None              # selected component uid
        self._sel_line = None             # (branch, insert_idx) or None
        self._tag_map  = {}              # canvas tag → component
        self._line_map = {}              # canvas item → (branch, insert_idx)

        # Canvas + scrollbars
        self.canvas = tk.Canvas(self, bg='#FDFEFE', cursor='arrow',
                                highlightthickness=1, highlightbackground='#BDC3C7')
        vsb = ttk.Scrollbar(self, orient='vertical',   command=self.canvas.yview)
        hsb = ttk.Scrollbar(self, orient='horizontal', command=self.canvas.xview)
        self.canvas.config(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.canvas.bind('<Button-1>', self._on_click)

    # ── public API ─────────────────────────────────────────────────────────

    def draw(self, network, selected_comp=None, selected_line=None):
        """Redraw the entire network schematic."""
        self._net = network
        self._sel = selected_comp.uid if selected_comp else None
        self._sel_line = selected_line   # (branch, insert_idx) or None
        self._tag_map.clear()
        self._line_map.clear()
        c = self.canvas
        c.delete('all')

        n_branches = len(network.branches)
        max_comps  = max((len(b.comps) for b in network.branches), default=0)

        # ── Dynamic branch_start_x (shifted right when HX present) ───────
        if network.use_hx:
            branch_start_x = HX_BRANCH_START_X
        else:
            branch_start_x = BRANCH_START_X
        bus_left_x = branch_start_x - 8   # pre-compute for HX wire endpoint

        # Dynamic canvas size — extra height for the return-line route below branches
        cw = max(800, branch_start_x + (max_comps + 1) * (COMP_W + COMP_GAP) + RIGHT_PAD + 160)
        ch = max(340, TOP_PAD * 2 + n_branches * ROW_H + ROW_H // 2 + 60)
        c.config(scrollregion=(0, 0, cw, ch))

        pump_cy = ch // 2

        # ── Header tank (above pump) ─────────────────────────────────────
        if network.use_tank:
            tx = LEFT_PAD
            ty = pump_cy - PUMP_H // 2 - 80
            self._draw_box(tx, ty, PUMP_W, 60,
                           TankComp.COLOR, network.tank.canvas_label(),
                           tag=f"tank_{network.tank.uid}",
                           comp=network.tank)
            c.create_line(tx + PUMP_W // 2, ty + 60,
                          tx + PUMP_W // 2, pump_cy - PUMP_H // 2,
                          fill='#6C3483', width=2, arrow='last')

        # ── Pump ────────────────────────────────────────────────────────
        px = LEFT_PAD
        py = pump_cy - PUMP_H // 2
        self._draw_box(px, py, PUMP_W, PUMP_H,
                       PumpComp.COLOR, network.pump.canvas_label(),
                       tag=f"pump_{network.pump.uid}", comp=network.pump,
                       bold=True)

        # ── Heat Exchanger (between pump and manifold) ───────────────────
        if network.use_hx:
            hx = network.hx
            hx_sel = (selected_comp is not None and
                      isinstance(selected_comp, HeatExchangerComp))
            hx_x   = LEFT_PAD + PUMP_W + HX_GAP
            hx_y   = pump_cy - COMP_H // 2 - 8
            hx_h   = COMP_H + 16   # slightly taller than components
            # Wire pump → HX
            c.create_line(LEFT_PAD + PUMP_W, pump_cy,
                          hx_x, pump_cy,
                          fill='#2C3E50', width=2, arrow='last')
            # HX box
            hx_label = hx.canvas_label()
            if network.T_manifold is not None:
                hx_label += (f"\nIn {hx.T_water_in:.1f}°C → {hx.T_water_out:.1f}°C"
                             f"\nGly → {hx.T_glycol_out:.1f}°C  {hx.Q_duty:.0f} W")
            outline_hx = '#F39C12' if hx_sel else '#2C3E50'
            lw_hx = 3 if hx_sel else 1
            r = c.create_rectangle(hx_x, hx_y, hx_x + HX_W, hx_y + hx_h,
                                   fill=HeatExchangerComp.COLOR,
                                   outline=outline_hx, width=lw_hx,
                                   tags=('hx_box',))
            t = c.create_text(hx_x + HX_W // 2, hx_y + hx_h // 2,
                              text=hx_label, fill='white',
                              font=('Arial', 6, 'bold'), justify='center',
                              tags=('hx_box',))
            self._tag_map['hx_box'] = hx
            for item in (r, t):
                c.tag_bind(item, '<Button-1>',
                           lambda e, cp=hx: self._click_comp(cp))
            # Wire HX → manifold / bus
            hx_wire_end = bus_left_x if n_branches > 1 else branch_start_x
            c.create_line(hx_x + HX_W, pump_cy,
                          hx_wire_end, pump_cy,
                          fill='#2C3E50', width=2, arrow='last')

        # ── Compute branch Y positions ───────────────────────────────────
        if n_branches == 1:
            branch_ys = [ch // 2]
        else:
            span = (n_branches - 1) * ROW_H
            top  = ch // 2 - span // 2
            branch_ys = [top + i * ROW_H for i in range(n_branches)]

        # Return box sits at the right, centred vertically
        ret_x  = cw - RIGHT_PAD - 110
        ret_cy = ch // 2

        # ── Bus wires for parallel branches ──────────────────────────────
        bus_left  = branch_start_x - 8   # vertical bus x (left side)
        bus_right = ret_x - 8            # vertical bus x (right side)

        if n_branches > 1:
            # Horizontal pump/HX outlet → left bus
            if not network.use_hx:
                c.create_line(LEFT_PAD + PUMP_W, pump_cy,
                              bus_left, pump_cy,
                              fill='#2C3E50', width=3)
            # Left vertical bus
            c.create_line(bus_left, branch_ys[0],
                          bus_left, branch_ys[-1],
                          fill='#2C3E50', width=3)
            # Right vertical bus
            c.create_line(bus_right, branch_ys[0],
                          bus_right, branch_ys[-1],
                          fill='#2C3E50', width=3)
            # Right bus → return box
            c.create_line(bus_right, ret_cy,
                          ret_x, ret_cy,
                          fill='#2C3E50', width=3, arrow='last')
        else:
            # Single branch: straight line from source to branch start
            if not network.use_hx:
                c.create_line(LEFT_PAD + PUMP_W, pump_cy,
                              branch_start_x, pump_cy,
                              fill='#2C3E50', width=3)

        # ── Temperature label at manifold (left bus / branch start) ──────
        if network.T_manifold is not None:
            c.create_text(branch_start_x + 2, pump_cy - PUMP_H // 2 - 8,
                          text=f"T_manifold\n{network.T_manifold:.1f} °C",
                          fill='#117A65', font=('Arial', 7, 'bold'), anchor='w')

        # ── Branches ────────────────────────────────────────────────────
        for branch, by in zip(network.branches, branch_ys):
            self._draw_branch(branch, by, pump_cy, ret_x, ret_cy,
                              bus_left, bus_right, n_branches > 1,
                              branch_start_x)

        # ── RETURN box ──────────────────────────────────────────────────
        ret_label = 'RETURN\n(reservoir)'
        if network.T_return is not None:
            ret_label += f"\nT={network.T_return:.1f} °C"
        self._draw_box(ret_x, ret_cy - 30, 110, 60,
                       '#7F8C8D', ret_label,
                       tag='return_node')

        # ── Branch labels ────────────────────────────────────────────────
        for branch, by in zip(network.branches, branch_ys):
            c.create_text(branch_start_x, by - COMP_H // 2 - 8,
                          text=branch.name, fill='#5D6D7E',
                          font=('Arial', 8, 'italic'), anchor='w')

        # ── Return line: RESERVOIR → PUMP (closed loop) ──────────────────
        ret_cx   = ret_x + 55
        ret_bot  = ret_cy + 30
        pump_cx  = LEFT_PAD + PUMP_W // 2
        pump_bot = pump_cy + PUMP_H // 2
        route_y  = max(branch_ys[-1] if branch_ys else ret_cy, ret_bot) + 28
        route_y  = max(route_y, ret_bot + 20)

        dash = (6, 4)
        ret_line_color = '#5DADE2'
        c.create_line(ret_cx, ret_bot, ret_cx, route_y,
                      fill=ret_line_color, width=2, dash=dash)
        c.create_line(ret_cx, route_y, pump_cx, route_y,
                      fill=ret_line_color, width=2, dash=dash)
        c.create_line(pump_cx, route_y, pump_cx, pump_bot,
                      fill=ret_line_color, width=2, dash=dash, arrow='last')
        # Small "RETURN" label on the bottom run
        c.create_text((ret_cx + pump_cx) // 2, route_y - 8,
                      text="return line", fill=ret_line_color,
                      font=('Arial', 7, 'italic'))

    def _draw_branch(self, branch, by, pump_cy, ret_x, ret_cy,
                     bus_left, bus_right, has_siblings,
                     branch_start_x=None):
        c  = self.canvas
        if branch_start_x is None:
            branch_start_x = BRANCH_START_X
        cx = branch_start_x

        # ── Initial connection (before first component = insert pos 0) ───────
        if has_siblings:
            is_sel0 = (self._sel_line is not None and
                       self._sel_line[0] is branch and
                       self._sel_line[1] == 0)
            # Hit zone rectangle drawn FIRST so the visible line can be on top
            hit0 = c.create_rectangle(bus_left, by - 10, cx, by + 10,
                                      fill='#FEF9E7' if is_sel0 else '',
                                      outline='#F39C12' if is_sel0 else '')
            self._register_gap(hit0, branch, 0)
            # Visible line drawn AFTER the rectangle — always shows on top
            c.create_line(bus_left, by, cx, by,
                          fill='#F39C12' if is_sel0 else '#2C3E50', width=2)
        # else: single-branch lead line is drawn in draw() — we register it separately

        for comp_idx, comp in enumerate(branch.comps):
            selected = (comp.uid == self._sel)
            tag = f"comp_{comp.uid}"
            self._draw_comp_box(cx, by - COMP_H // 2, COMP_W, COMP_H,
                                comp, tag, selected)

            # ── Gap after this component (insert pos = comp_idx + 1) ─────────
            insert_pos = comp_idx + 1
            gap_x1 = cx + COMP_W
            gap_x2 = cx + COMP_W + COMP_GAP
            is_sel_gap = (self._sel_line is not None and
                          self._sel_line[0] is branch and
                          self._sel_line[1] == insert_pos)

            # Highlight rectangle under the gap (drawn first so arrow is on top)
            hit = c.create_rectangle(gap_x1, by - COMP_H // 2,
                                     gap_x2, by + COMP_H // 2,
                                     fill='#FEF9E7' if is_sel_gap else '',
                                     outline='#F39C12' if is_sel_gap else '')
            if is_sel_gap:
                c.create_text(gap_x1 + COMP_GAP // 2, by - COMP_H // 2 - 7,
                              text='✚ INSERT', fill='#F39C12',
                              font=('Arial', 6, 'bold'))
            self._register_gap(hit, branch, insert_pos)

            # Arrow on top
            c.create_line(gap_x1, by, gap_x2, by,
                          fill='#F39C12' if is_sel_gap else '#2C3E50',
                          width=2, arrow='last')
            cx += COMP_W + COMP_GAP

        # ── Branch flow indicator badge ───────────────────────────────────────
        q_lpm = m3s_to_lpm(branch.comps[0].q_m3s) if branch.comps else 0.0
        if q_lpm > 0.001:
            badge_x = (bus_right - 52) if has_siblings else (ret_x - 56)
            bw, bh = 52, 18
            c.create_rectangle(badge_x, by - bh - 2, badge_x + bw, by - 2,
                                fill='#1A5276', outline='#5DADE2', width=1)
            c.create_text(badge_x + bw // 2, by - bh // 2 - 2,
                          text=f"▶ {q_lpm:.2f} L/min",
                          fill='#AED6F1', font=('Arial', 7, 'bold'),
                          justify='center')

        # ── Final connection (after last component = insert at end) ───────────
        insert_end = len(branch.comps)
        is_sel_end = (self._sel_line is not None and
                      self._sel_line[0] is branch and
                      self._sel_line[1] == insert_end)

        if has_siblings:
            # Hit zone rectangle drawn FIRST, visible line on top
            hit_end = c.create_rectangle(cx, by - 10, bus_right, by + 10,
                                         fill='#FEF9E7' if is_sel_end else '',
                                         outline='#F39C12' if is_sel_end else '')
            self._register_gap(hit_end, branch, insert_end)
            c.create_line(cx, by, bus_right, by,
                          fill='#F39C12' if is_sel_end else '#2C3E50', width=2)
        else:
            # Hit zone rectangle drawn FIRST, visible line on top
            hit_end = c.create_rectangle(cx, by - 10, ret_x, by + 10,
                                         fill='#FEF9E7' if is_sel_end else '',
                                         outline='#F39C12' if is_sel_end else '')
            self._register_gap(hit_end, branch, insert_end)
            c.create_line(cx, by, ret_x, by,
                          fill='#F39C12' if is_sel_end else '#2C3E50',
                          width=2, arrow='last')

    def _draw_comp_box(self, x, y, w, h, comp, tag, selected):
        """Draw a single component box with post-sim overlay."""
        c       = self.canvas
        outline = '#F39C12' if selected else '#2C3E50'
        lwidth  = 3 if selected else 1

        # Colour: grey if no flow yet, else component colour
        q_lpm = m3s_to_lpm(comp.q_m3s)
        fill  = '#BDC3C7' if comp.q_m3s == 0.0 else comp.COLOR

        tags = (tag,)
        rect = c.create_rectangle(x, y, x + w, y + h,
                                   fill=fill, outline=outline,
                                   width=lwidth, tags=tags)

        # Full text centred in box
        txt = c.create_text(x + w // 2, y + h // 2 - 4,
                             text=comp.canvas_label(),
                             fill='white', font=('Arial', 7),
                             justify='center', tags=tags)

        # Post-simulation overlay at bottom edge of box
        if q_lpm > 0.0:
            if isinstance(comp, HeatSourceComp) and comp.T_out > 0.0:
                # Show flow + outlet temperature for heat sources
                overlay = f"Q={q_lpm:.1f}L/min  T_out={comp.T_out:.1f}°C"
            else:
                dp_mbar = pa_to_bar(comp.dp_pa) * 1000
                overlay = f"Q={q_lpm:.1f}L/min  ΔP={dp_mbar:.0f}mb"
            c.create_rectangle(x + 1, y + h - 14, x + w - 1, y + h - 1,
                                fill='#1A252F', outline='', tags=tags)
            c.create_text(x + w // 2, y + h - 7,
                           text=overlay, fill='#F0F3F4',
                           font=('Arial', 6), justify='center', tags=tags)

        if tag:
            self._tag_map[tag] = comp
            for item in (rect, txt):
                c.tag_bind(item, '<Button-1>',
                           lambda e, cp=comp: self._click_comp(cp))

    def _draw_box(self, x, y, w, h, fill, text,
                  tag=None, comp=None, selected=False, bold=False):
        """Generic box (pump, tank, return)."""
        c = self.canvas
        outline = '#F39C12' if selected else '#2C3E50'
        lwidth  = 3 if selected else 1
        tags    = (tag,) if tag else ()

        rect = c.create_rectangle(x, y, x + w, y + h,
                                   fill=fill, outline=outline,
                                   width=lwidth, tags=tags)
        font = ('Arial', 8, 'bold') if bold else ('Arial', 8)
        txt  = c.create_text(x + w // 2, y + h // 2, text=text,
                              fill='white', font=font, justify='center',
                              tags=tags)
        if comp is not None and tag:
            self._tag_map[tag] = comp
            for item in (rect, txt):
                c.tag_bind(item, '<Button-1>',
                           lambda e, cp=comp: self._click_comp(cp))

    def _on_click(self, event):
        """Handle canvas click on empty background — deselect everything."""
        items = self.canvas.find_overlapping(
            event.x - 2, event.y - 2, event.x + 2, event.y + 2)
        if not items:
            self._sel_line = None
            self.on_select(None)

    def _click_comp(self, comp):
        self._sel = comp.uid
        self._sel_line = None    # deselect any line
        self.on_select(comp)

    def _register_gap(self, item, branch, insert_idx):
        """Register a canvas item as a clickable line-segment insertion point."""
        c = self.canvas
        self._line_map[item] = (branch, insert_idx)
        c.tag_bind(item, '<Button-1>',
                   lambda e, b=branch, i=insert_idx: self._click_line(b, i))
        c.tag_bind(item, '<Enter>', lambda e: c.config(cursor='crosshair'))
        c.tag_bind(item, '<Leave>', lambda e: c.config(cursor='arrow'))

    def _click_line(self, branch, insert_idx):
        """Called when a line-gap hit zone is clicked."""
        self._sel      = None    # deselect any component
        self._sel_line = (branch, insert_idx)
        self.on_line_select(branch, insert_idx)


class WaterFlowApp:
    """Main application window."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("TATTOOS – Water Flow Network Simulator")
        self.root.geometry("1280x820")
        self.root.minsize(900, 600)

        self.network  = HydroNetwork()
        self.sel_comp = None
        self.sel_line = None   # (branch, insert_idx) or None

        # ── Pre-populate a demo network so something is visible at startup ──
        b = self.network.branches[0]
        b.name = "Branch 1"
        b.comps = [
            PipeComp(d_mm=15, l_m=2.0),
            ValveComp(d_mm=15, pct=25.0),   # 25% open – noticeable pressure drop
            HeatSourceComp("Target A", Q_W=150.0, dp_pa=3000, qref_lpm=5.0),
            PipeComp(d_mm=15, l_m=1.0),
        ]
        b2 = self.network.add_branch()
        b2.name = "Branch 2"
        b2.comps = [
            PipeComp(d_mm=10, l_m=3.0),
            HeatSourceComp("Target B", Q_W=80.0, dp_pa=2000, qref_lpm=3.0),
        ]

        # Enable heat exchanger with sensible defaults
        self.network.use_hx = True
        self.network.hx.T_glycol_in  = 15.0
        self.network.hx.Q_glycol_lpm = 8.0
        self.network.hx.UA           = 300.0
        self.network.hx.dp_pa        = 200.0

        self.sel_branch = b

        self._build_ui()
        # Auto-run simulation after the mainloop starts
        self.root.after(50, self._run_sim)

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Top toolbar ─────────────────────────────────────────────────
        toolbar = tk.Frame(self.root, bg='#2C3E50', pady=4)
        toolbar.pack(fill='x')
        self._build_toolbar(toolbar)

        # ── Main content ────────────────────────────────────────────────
        pane = tk.PanedWindow(self.root, orient='vertical', sashwidth=5,
                              sashrelief='raised')
        pane.pack(fill='both', expand=True)

        upper = tk.PanedWindow(pane, orient='horizontal', sashwidth=5,
                               sashrelief='raised')
        pane.add(upper, minsize=300)

        # Canvas (left, larger portion)
        self.canvas_view = CanvasView(upper, self._on_select,
                                      on_line_select=self._on_line_select)
        upper.add(self.canvas_view, minsize=500)

        # Right side: branch selector + properties (narrower)
        right = tk.Frame(upper, width=300)
        right.pack_propagate(False)
        upper.add(right, minsize=260)
        self._build_right_panel(right)

        # Results table (bottom)
        self.results_panel = ResultsPanel(pane)
        pane.add(self.results_panel, minsize=160)

        # Set initial sash positions after window is drawn
        def _set_sash():
            try:
                total_w = self.root.winfo_width()
                upper.sash_place(0, max(700, total_w - 310), 0)
            except Exception:
                pass
        self.root.after(100, _set_sash)

    def _build_toolbar(self, parent):
        """Top toolbar with pump/tank settings and component buttons."""
        def sep():
            tk.Label(parent, text=" │ ", bg='#2C3E50',
                     fg='#7F8C8D').pack(side='left')

        # ── Pump settings ──────────────────────────────────────────────
        tk.Label(parent, text="  Pump:", bg='#2C3E50',
                 fg='white', font=('Arial', 9, 'bold')).pack(side='left')

        tk.Label(parent, text="P =", bg='#2C3E50', fg='#BDC3C7').pack(side='left', padx=(4, 0))
        self.v_pump_p = tk.DoubleVar(value=1.0)
        tk.Spinbox(parent, textvariable=self.v_pump_p,
                   from_=0.1, to=20.0, increment=0.1, format='%.1f',
                   width=5, command=self._pump_changed).pack(side='left', padx=2)
        tk.Label(parent, text="bar", bg='#2C3E50', fg='#BDC3C7').pack(side='left')

        tk.Label(parent, text=" Q_max =", bg='#2C3E50', fg='#BDC3C7').pack(side='left', padx=(6, 0))
        self.v_pump_q = tk.DoubleVar(value=20.0)
        tk.Spinbox(parent, textvariable=self.v_pump_q,
                   from_=0.5, to=500.0, increment=0.5, format='%.1f',
                   width=6, command=self._pump_changed).pack(side='left', padx=2)
        tk.Label(parent, text="L/min", bg='#2C3E50', fg='#BDC3C7').pack(side='left')

        sep()

        # ── Header tank ────────────────────────────────────────────────
        self.v_use_tank = tk.BooleanVar(value=False)
        tk.Checkbutton(parent, text="Header Tank", variable=self.v_use_tank,
                       bg='#2C3E50', fg='white', selectcolor='#1A252F',
                       activebackground='#2C3E50',
                       command=self._tank_toggled).pack(side='left', padx=4)
        tk.Label(parent, text="h =", bg='#2C3E50', fg='#BDC3C7').pack(side='left')
        self.v_tank_h = tk.DoubleVar(value=2.0)
        tk.Spinbox(parent, textvariable=self.v_tank_h,
                   from_=0.1, to=30.0, increment=0.1, format='%.1f',
                   width=5, command=self._tank_changed).pack(side='left', padx=2)
        tk.Label(parent, text="m", bg='#2C3E50', fg='#BDC3C7').pack(side='left')

        sep()

        # ── Heat exchanger toggle ──────────────────────────────────────
        self.v_use_hx = tk.BooleanVar(value=self.network.use_hx)
        tk.Checkbutton(parent, text="HX", variable=self.v_use_hx,
                       bg='#2C3E50', fg='white', selectcolor='#1A252F',
                       activebackground='#2C3E50',
                       command=self._hx_toggled).pack(side='left', padx=(4, 2))
        tk.Label(parent, text="(click HX box to edit)", bg='#2C3E50',
                 fg='#7F8C8D', font=('Arial', 7, 'italic')).pack(side='left')

        sep()

        # ── Add-component buttons ──────────────────────────────────────
        btn = {'relief': 'flat', 'pady': 2, 'padx': 8, 'bd': 0}
        tk.Button(parent, text="+ Pipe",        bg='#2980B9', fg='white',
                  command=lambda: self._add_comp('pipe'),        **btn).pack(side='left', padx=2)
        tk.Button(parent, text="+ Valve",       bg='#27AE60', fg='white',
                  command=lambda: self._add_comp('valve'),       **btn).pack(side='left', padx=2)
        tk.Button(parent, text="+ Heat Source", bg='#E74C3C', fg='white',
                  command=lambda: self._add_comp('heatsource'),  **btn).pack(side='left', padx=2)

        sep()

        # ── Branch management ──────────────────────────────────────────
        tk.Button(parent, text="+ Branch",    bg='#16A085', fg='white',
                  command=self._add_branch,    **btn).pack(side='left', padx=2)
        tk.Button(parent, text="– Branch",    bg='#8E44AD', fg='white',
                  command=self._remove_branch, **btn).pack(side='left', padx=2)

        # ── Delete / Run ────────────────────────────────────────────────
        tk.Button(parent, text="🗑 Delete",       bg='#C0392B', fg='white',
                  command=self._delete_selected,  **btn).pack(side='right', padx=4)
        tk.Button(parent, text="▶  Run Simulation",
                  bg='#F39C12', fg='white', font=('Arial', 9, 'bold'),
                  command=self._run_sim,           **btn).pack(side='right', padx=8)

    def _build_right_panel(self, parent):
        """Branch selector list + properties panel."""
        tk.Label(parent, text="Parallel Branches",
                 font=('Arial', 9, 'bold'), bg='#2C3E50',
                 fg='white').pack(fill='x')

        # Branch listbox
        lf = tk.Frame(parent)
        lf.pack(fill='x', padx=4, pady=(4, 0))
        sb = ttk.Scrollbar(lf, orient='vertical')
        self.branch_lb = tk.Listbox(lf, height=5, yscrollcommand=sb.set,
                                    selectmode='single', font=('Arial', 9))
        sb.config(command=self.branch_lb.yview)
        self.branch_lb.pack(side='left', fill='x', expand=True)
        sb.pack(side='right', fill='y')
        self.branch_lb.bind('<<ListboxSelect>>', self._on_branch_select)

        # Properties panel
        self.props = PropertiesPanel(parent, self._on_apply)
        self.props.pack(fill='both', expand=True, padx=4, pady=4)

    # ── Callbacks ───────────────────────────────────────────────────────────

    def _on_select(self, comp):
        self.sel_comp = comp
        self.sel_line = None          # deselect any line
        self.props.load(comp)
        self.canvas_view.draw(self.network, comp)

    def _on_apply(self):
        """Properties panel applied – refresh everything."""
        # Sync pump spinboxes if pump was edited via properties
        self.v_pump_p.set(self.network.pump.pbar)
        self.v_pump_q.set(self.network.pump.qmax)
        self._refresh()

    def _on_line_select(self, branch, insert_idx):
        """Called when a line segment (gap) on the canvas is clicked."""
        self.sel_comp = None
        self.sel_line = (branch, insert_idx)
        # Also update the branch selector to reflect the branch of the clicked line
        if branch in self.network.branches:
            self.sel_branch = branch
        self.props.load_line_info(branch, insert_idx,
                                  on_insert=lambda kind: self._insert_at(kind, branch, insert_idx))
        self.canvas_view.draw(self.network, selected_line=self.sel_line)

    def _insert_at(self, kind, branch, idx):
        """Insert a new component at *idx* in *branch* and select it."""
        if kind == 'pipe':
            comp = PipeComp()
        elif kind == 'valve':
            comp = ValveComp()
        elif kind in ('device', 'heatsource'):
            comp = HeatSourceComp()
        else:
            return
        branch.comps.insert(idx, comp)
        self.sel_line = None
        self.sel_comp = comp
        self._refresh()
        self.props.load(comp)

    def _on_branch_select(self, _event=None):
        idxs = self.branch_lb.curselection()
        if idxs:
            self.sel_branch = self.network.branches[idxs[0]]
        self._refresh()

    def _pump_changed(self):
        try:
            self.network.pump.pbar = float(self.v_pump_p.get())
            self.network.pump.qmax = float(self.v_pump_q.get())
        except (ValueError, tk.TclError):
            pass
        self._refresh()

    def _tank_toggled(self):
        self.network.use_tank = self.v_use_tank.get()
        self._tank_changed()

    def _tank_changed(self):
        try:
            self.network.tank.h_m = float(self.v_tank_h.get())
        except (ValueError, tk.TclError):
            pass
        self._refresh()

    def _hx_toggled(self):
        self.network.use_hx = self.v_use_hx.get()
        self._refresh()

    def _add_comp(self, kind):
        """Add a component to the currently selected branch.

        If a line segment is selected, inserts at that exact position.
        Otherwise appends to the end of the selected branch.
        """
        if kind == 'pipe':
            c = PipeComp()
        elif kind == 'valve':
            c = ValveComp()
        elif kind in ('device', 'heatsource'):
            c = HeatSourceComp()
        else:
            return
        if self.sel_line is not None:
            branch, idx = self.sel_line
            branch.comps.insert(idx, c)
            self.sel_line = None
        else:
            self.sel_branch.comps.append(c)
        self.sel_comp = c
        self._refresh()
        self.props.load(c)

    def _add_branch(self):
        b = self.network.add_branch()
        # Add a default pipe so the branch is not empty
        b.comps.append(PipeComp())
        self.sel_branch = b
        self._refresh()

    def _remove_branch(self):
        if len(self.network.branches) <= 1:
            messagebox.showinfo("Info", "Cannot remove the last branch.")
            return
        self.network.remove_branch(self.sel_branch)
        self.sel_branch = self.network.branches[0]
        self.sel_comp   = None
        self.props.load(None)
        self._refresh()

    def _delete_selected(self):
        if self.sel_comp is None:
            messagebox.showinfo("Info", "Select a component first.")
            return
        for b in self.network.branches:
            if self.sel_comp in b.comps:
                b.comps.remove(self.sel_comp)
                break
        self.sel_comp = None
        self.props.load(None)
        self._refresh()

    def _run_sim(self):
        """Run simulation and update all displays."""
        results = self.network.simulate()
        self.results_panel.update(self.network, results)
        # Refresh properties panel result string
        self.props.refresh_results(self.sel_comp)
        self._refresh(run=True)

    # ── Internal helpers ────────────────────────────────────────────────────

    def _refresh(self, run=False):
        """Redraw canvas and update branch listbox."""
        # Branch listbox
        self.branch_lb.delete(0, 'end')
        sel_idx = 0
        for i, b in enumerate(self.network.branches):
            # Flow through a series branch = flow through any one component
            # (not the sum – each component carries the same flow)
            q_lpm = m3s_to_lpm(b.comps[0].q_m3s) if b.comps else 0.0
            n     = len(b.comps)
            label = f"{b.name}  ({n} comp)  {q_lpm:.2f} L/min" if run else \
                    f"{b.name}  ({n} comp)"
            self.branch_lb.insert('end', label)
            if b is self.sel_branch:
                sel_idx = i

        self.branch_lb.selection_set(sel_idx)

        # Sync toolbar vars → network model
        self.network.pump.pbar = self.v_pump_p.get()
        self.network.pump.qmax = self.v_pump_q.get()
        self.network.tank.h_m  = self.v_tank_h.get()
        self.network.use_hx    = self.v_use_hx.get()

        self.canvas_view.draw(self.network, self.sel_comp,
                              selected_line=self.sel_line)

    # ── Entry point ─────────────────────────────────────────────────────────

    def run(self):
        self.root.mainloop()


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    app = WaterFlowApp()
    app.run()
