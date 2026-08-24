#!/usr/bin/env python3
"""Generate, validate, execute, and inspect the 20-flight qualification dataset.

The trajectory catalog is simulator-truth based and importable without ROS.  Live
execution is deliberately standalone: it publishes PX4 offboard setpoints itself
and does not import the III mission or control implementations.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import numpy as np
import yaml


ROOT = Path(__file__).resolve().parents[2]
GEOMETRY = ROOT / "src/III-Drone-Simulation/Gazebo-simulation-assets/world_models/hcaa_pylon_setup/conductors.yaml"
OUTPUT = ROOT / "datasets/powerline_qualification"
SETPOINT_HZ = 50.0
DENSE_HZ = 250.0
RECORD_TOPICS = tuple(__import__("perception_dataset_flights").RECORD_TOPICS)
REQUESTED_D_MIN_M = 2.5
REQUESTED_D_MAX_M = 8.0
EXECUTED_D_MIN_M = 0.5
EXECUTED_D_MAX_M = 1.5


def executed_clearance(requested_d):
    """Approved safety mapping; affine so analytic derivatives remain exact."""
    scale = (EXECUTED_D_MAX_M-EXECUTED_D_MIN_M)/(REQUESTED_D_MAX_M-REQUESTED_D_MIN_M)
    return EXECUTED_D_MIN_M + (np.asarray(requested_d)-REQUESTED_D_MIN_M)*scale


def executed_clearance_derivative(requested_d_derivative):
    scale = (EXECUTED_D_MAX_M-EXECUTED_D_MIN_M)/(REQUESTED_D_MAX_M-REQUESTED_D_MIN_M)
    return np.asarray(requested_d_derivative)*scale


FLIGHTS = [
    *(dict(flight_id=f"CAL{i:02d}", split="calibration", seed=1000 + i) for i in range(1, 13)),
    *(dict(flight_id=f"VAL{i:02d}", split="validation", seed=2000 + i) for i in range(1, 5)),
    *(dict(flight_id=f"TEST{i:02d}", split="test", seed=3000 + i) for i in range(1, 5)),
]
POSTFIX_OUTPUT = ROOT / "datasets/powerline_qualification_postfix_test"
POST_FLIGHTS = [dict(flight_id=f"POSTTEST{i:02d}", split="", seed=4000+i) for i in range(1, 6)]
FINAL_CANARY_OUTPUT = ROOT / "datasets/powerline_qualification_final_canary"
CANARY_FLIGHTS = [dict(flight_id="CANARY", split="", seed=5099)]
FINAL_TEST_OUTPUT = ROOT / "datasets/powerline_qualification_final_test"
FINAL_FLIGHTS = [dict(flight_id=f"FINALTEST{i:02d}", split="", seed=5000+i) for i in range(1,6)]
ALL_FLIGHTS = FLIGHTS + POST_FLIGHTS + CANARY_FLIGHTS + FINAL_FLIGHTS


def smoother(t):
    t = np.asarray(t)
    return 10*t**3 - 15*t**4 + 6*t**5


def smoother_d(t):
    t = np.asarray(t)
    return 30*t**2 - 60*t**3 + 30*t**4


def smoother_dd(t):
    t = np.asarray(t)
    return 60*t - 180*t**2 + 120*t**3


@dataclasses.dataclass
class Corridor:
    ids: list[str]
    points: dict[str, np.ndarray]
    x_axis: np.ndarray
    y_axis: np.ndarray
    origin_xy: np.ndarray
    coefficients: dict[str, np.ndarray]
    x_limits: tuple[float, float]
    A: float

    @classmethod
    def load(cls, path: Path = GEOMETRY):
        raw = yaml.safe_load(path.read_text())
        points = {c["id"]: np.asarray(c["samples"], dtype=float) for c in raw["conductors"]}
        # Qualification convention is semantic, not YAML order: C0 is the
        # bottom conductor and numbering increases monotonically upward.
        ids = sorted(points, key=lambda conductor_id: float(np.mean(points[conductor_id][:, 2])))
        c0 = points[ids[0]]
        axis = c0[-1, :2] - c0[0, :2]
        axis /= np.linalg.norm(axis)
        yaxis = np.array([-axis[1], axis[0]])  # z_G cross x_G
        station0 = (c0[:, :2] - c0[0, :2]) @ axis
        coeff0 = np.polyfit(station0, c0[:, 2], 2)
        zero_station = -coeff0[1] / (2 * coeff0[0])
        origin_xy = c0[0, :2] + zero_station * axis
        coefficients = {}
        all_x = {}
        for conductor_id, xyz in points.items():
            x = (xyz[:, :2] - origin_xy) @ axis
            all_x[conductor_id] = x
            coefficients[conductor_id] = np.polyfit(x, xyz[:, 2], 2)
        limits = (float(all_x[ids[0]].min()), float(all_x[ids[0]].max()))
        A = min(20.0, 0.6 * min(abs(limits[0]), abs(limits[1])))
        return cls(ids, points, axis, yaxis, origin_xy, coefficients, limits, A)

    def z(self, conductor_id, x):
        return np.polyval(self.coefficients[conductor_id], x)

    def slope(self, conductor_id, x):
        a, b, _ = self.coefficients[conductor_id]
        return 2*a*np.asarray(x) + b

    def world(self, x, y, z):
        x, y, z = np.broadcast_arrays(x, y, z)
        xy = self.origin_xy + x[..., None]*self.x_axis + y[..., None]*self.y_axis
        return np.column_stack((xy.reshape(-1, 2), z.reshape(-1)))

    def conductor_G(self, conductor_id, n=1001):
        xyz = self.points[conductor_id]
        delta = xyz[:, :2] - self.origin_xy
        x = delta @ self.x_axis
        y = delta @ self.y_axis
        order = np.argsort(x)
        xx = np.linspace(x.min(), x.max(), n)
        yy = np.interp(xx, x[order], y[order])
        return np.column_stack((xx, yy, self.z(conductor_id, xx)))


@dataclasses.dataclass
class Trajectory:
    flight_id: str
    split: str
    seed: int
    name: str
    equations: list[str]
    t: np.ndarray
    p: np.ndarray
    v: np.ndarray
    yaw: np.ndarray
    yaw_rate: np.ndarray
    phase: list[str]
    safety_adaptations: list[str] = dataclasses.field(default_factory=list)


class Builder:
    def __init__(self, corridor: Corridor, dt=1/SETPOINT_HZ):
        self.g = corridor
        self.dt = dt
        self.t=[]; self.p=[]; self.v=[]; self.yaw=[]; self.yr=[]; self.phase=[]

    def append(self, duration, fn: Callable, label, include_first=False):
        n=max(2, int(round(duration/self.dt))+1)
        local=np.linspace(0,duration,n)
        if self.t and not include_first: local=local[1:]
        base=self.t[-1] if self.t else 0.0
        for q in local:
            p,v,y,r=fn(float(q))
            self.t.append(base+q); self.p.append(p); self.v.append(v)
            self.yaw.append(y); self.yr.append(r); self.phase.append(label)

    def hold(self, x,y,d,yaw,duration,label):
        de=float(executed_clearance(d))
        self.append(duration, lambda _: (np.array([x,y,self.g.z(self.g.ids[0],x)-de]),np.zeros(3),yaw,0.),label)

    def transition(self, start, end, duration, label):
        p0=np.asarray(start[:3],float); p1=np.asarray(end[:3],float); y0=start[3]; y1=end[3]
        def fn(q):
            u=q/duration; s=smoother(u); sd=smoother_d(u)/duration
            return p0+(p1-p0)*s,(p1-p0)*sd,y0+(y1-y0)*s,(y1-y0)*sd
        self.append(duration,fn,label)

    def transition_limited(self, start, end, nominal_speed, label, acceleration=1.5):
        delta=np.asarray(end[:3],float)-np.asarray(start[:3],float)
        distance=float(np.linalg.norm(delta))
        # Quintic smootherstep has max |s''| = 10/sqrt(3) approximately.
        duration=max(1.0,1.875*distance/max(nominal_speed,1e-6),math.sqrt((10/math.sqrt(3))*distance/acceleration))
        self.transition(start,end,duration,label)

    def traverse(self,x0,x1,vmax,shape,label,acc_nominal=1.5):
        distance=abs(x1-x0); sign=np.sign(x1-x0)
        ta=1.875*vmax/acc_nominal
        adaptation=None
        if vmax*ta > 0.8*distance:
            ta=0.8*distance/vmax
            actual=1.875*vmax/ta
            adaptation=f"endpoint ramp peak acceleration raised from nominal {acc_nominal:.2f} to {actual:.3f} m/s^2 to attain requested {vmax:.2f} m/s within fixed span"
        ramp_distance=0.5*vmax*ta
        cruise_distance=distance-2*ramp_distance
        tc=max(0.0,cruise_distance/vmax)
        total=2*ta+tc
        def xstate(q):
            if q <= ta:
                u=q/ta
                vel=vmax*smoother(u)
                # integral of smoother: 2.5u4 - 3u5 + u6
                pos=vmax*ta*(2.5*u**4-3*u**5+u**6)
            elif q <= ta+tc:
                vel=vmax; pos=ramp_distance+vmax*(q-ta)
            else:
                u=(q-ta-tc)/ta
                vel=vmax*(1-smoother(u))
                pos=ramp_distance+cruise_distance+vmax*ta*(u-(2.5*u**4-3*u**5+u**6))
            return x0+sign*pos,sign*vel
        self.append(total,lambda q: shape(*xstate(q)),label)
        return adaptation

    def velocity_transition_distance(self,x0,distance,v0,v1,shape,label):
        duration=2*abs(distance)/max(v0+v1,1e-9); sign=np.sign(distance)
        def fn(q):
            u=q/duration; integral=2.5*u**4-3*u**5+u**6
            return shape(x0+sign*(v0*q+(v1-v0)*duration*integral),sign*(v0+(v1-v0)*smoother(u)))
        self.append(duration,fn,label)
        return x0+distance

    def constant_velocity_distance(self,x0,distance,speed,shape,label):
        duration=abs(distance)/speed; sign=np.sign(distance)
        self.append(duration,lambda q: shape(x0+sign*speed*q,sign*speed),label)
        return x0+distance

    def result(self, spec,name,equations,adaptations=()):
        return Trajectory(spec["flight_id"],spec["split"],spec["seed"],name,equations,
            np.asarray(self.t),np.asarray(self.p),np.asarray(self.v),np.asarray(self.yaw),np.asarray(self.yr),self.phase,list(filter(None,adaptations)))


def build_flight(spec, g: Corridor, dt=1/SETPOINT_HZ):
    b=Builder(g,dt); A=g.A; fid=spec["flight_id"]; c0=g.ids[0]; eq=[]; adaptations=[]
    rad=math.pi/180
    def hold_common(x=0,y=0,d=4,yaw=0,duration=5,label="initial_hover"): b.hold(x,y,d,yaw,duration,label)
    def constant_shape(y=0,d=4,yaw=0):
        de=float(executed_clearance(d))
        return lambda x,xd: (np.array([x,y,g.z(c0,x)-de]),np.array([xd,0,g.slope(c0,x)*xd]),yaw,0.)
    if fid=="CAL01":
        hold_common(duration=60,label="static_baseline"); name="static_baseline"; eq=["p_G=[0,0,z_C0(0)-4]", "v_G=[0,0,0]", "yaw=0"]
    elif fid=="CAL02":
        name="range_cross_view_grid"; order=[(-2,3),(0,3),(2,3),(2,5),(0,5),(-2,5),(-2,7),(0,7),(2,7)]
        for i,(y,d) in enumerate(order):
            p=np.array([0,y,g.z(c0,0)-executed_clearance(d)])
            if i:
                prev=order[i-1]; p0=np.array([0,prev[0],g.z(c0,0)-executed_clearance(prev[1])])
                b.transition_limited((*p0,0),(*p,0),1.0,f"transition_{i:02d}")
            b.hold(0,y,d,0,12,f"dwell_{i+1:02d}")
        eq=["snake order: (-2,3),(0,3),(2,3),(2,5),(0,5),(-2,5),(-2,7),(0,7),(2,7)","transitions use quintic smootherstep; v=(p1-p0)s'(t/T)/T"]
    elif fid=="CAL03":
        name="attitude_excitation"; hold_common(duration=10,label="yaw_0")
        current=0.
        for j,target in enumerate([20*rad,-20*rad,0.]):
            p=np.array([0,0,g.z(c0,0)-executed_clearance(4)]); b.transition((*p,current),(*p,target),3,f"yaw_transition_{j+1}"); b.hold(0,0,4,target,10,f"yaw_dwell_{j+1}"); current=target
        # Position sinusoids produce bounded controller tilt without direct attitude/model commands.
        T=16.; amp=.62; omega=2*math.pi/4
        def excite(axis):
            def fn(q):
                # Starts and ends at rest.  The alternating cosine acceleration
                # produces both signs of the requested pitch/roll excitation.
                w=amp*(1-math.cos(omega*q)); wd=amp*omega*math.sin(omega*q)
                x=w if axis==0 else 0.; y=w if axis==1 else 0.; xd=wd if axis==0 else 0.; yd=wd if axis==1 else 0.
                return np.array([x,y,g.z(c0,x)-executed_clearance(4)]),np.array([xd,yd,g.slope(c0,x)*xd]),0.,0.
            return fn
        b.append(T,excite(0),"pitch_by_longitudinal_acceleration"); z0=g.z(c0,0)-executed_clearance(4); b.transition((0,0,z0,0),(0,0,z0,0),2,"settle")
        b.append(T,excite(1),"roll_by_lateral_acceleration"); hold_common(duration=5,label="terminal_hover")
        eq=["yaw stages use quintic transitions and 10 s dwells","pitch/roll excitation: q=0.62 sin(2*pi*t/4), qdot=0.62*(2*pi/4)cos(...); peak acceleration 1.53 m/s^2"]
    elif fid in {"CAL04","CAL05","CAL06","CAL07","CAL08","VAL01","VAL02"}:
        params={"CAL04":(-A,A,.5,0,4),"CAL05":(-A,A,1.5,0,4),"CAL06":(-A,A,3,0,4),"CAL07":(-A,A,5,0,4),"CAL08":(A,-A,1.5,0,4),"VAL01":(-A,A,1,1,5),"VAL02":(A,-A,2.25,-1,3.5)}
        x0,x1,speed,y,d=params[fid]; name="straight_traversal"; hold_common(x0,y,d,duration=5)
        adaptations.append(b.traverse(x0,x1,speed,constant_shape(y,d),"traversal")); hold_common(x1,y,d,duration=5,label="terminal_hover")
        eq=[f"x: {x0:.6f}->{x1:.6f}, smooth ramps, cruise={speed}",f"y={y}; z=z_C0(x)-{d}","v=[xdot,0,z_C0'(x)xdot]; yaw=0"]
    elif fid in {"CAL09","CAL10","CAL11","VAL03","TEST02"}:
        params={"CAL09":(1.5,"lateral_slalom"),"CAL10":(1.5,"vertical_wave"),"CAL11":(1.5,"combined_motion"),"VAL03":(3.5,"combined_validation"),"TEST02":(2.5,"aggressive_combined")}
        speed,name=params[fid]
        def values(u):
            if fid=="CAL09": return 2*np.sin(2*np.pi*u),4+0*u,0*u,2*np.pi/A*np.cos(2*np.pi*u),0*u,0*u
            if fid=="CAL10": return 0*u,4+1.5*np.sin(2*np.pi*u),0*u,0*u,3*np.pi*np.cos(2*np.pi*u),0*u
            if fid=="CAL11": return 1.5*np.sin(4*np.pi*u),4+np.sin(2*np.pi*u+np.pi/2),15*rad*np.sin(2*np.pi*u),6*np.pi*np.cos(4*np.pi*u),2*np.pi*np.cos(2*np.pi*u+np.pi/2),15*rad*2*np.pi*np.cos(2*np.pi*u)
            if fid=="VAL03": return 2*np.sin(3*np.pi*u),4.5+1.25*np.sin(2*np.pi*u),10*rad*np.sin(4*np.pi*u),6*np.pi*np.cos(3*np.pi*u),2.5*np.pi*np.cos(2*np.pi*u),40*np.pi*rad*np.cos(4*np.pi*u)
            return 2.5*np.sin(4*np.pi*u),4.5+2*np.sin(3*np.pi*u),20*rad*np.sin(2*np.pi*u),10*np.pi*np.cos(4*np.pi*u),6*np.pi*np.cos(3*np.pi*u),40*np.pi*rad*np.cos(2*np.pi*u)
        def shape(x,xd):
            u=(x+A)/(2*A); y,d,yaw,dydu,dddu,dyawdu=values(u); ud=xd/(2*A)
            de=executed_clearance(d); dde_du=executed_clearance_derivative(dddu)
            return np.array([x,y,g.z(c0,x)-de]),np.array([xd,dydu*ud,(g.slope(c0,x)-dde_du/(2*A))*xd]),yaw,dyawdu*ud
        y0,d0,yaw0,*_=values(0); y1,d1,yaw1,*_=values(1)
        hold_common(-A,float(y0),float(d0),float(yaw0),5); adaptations.append(b.traverse(-A,A,speed,shape,"traversal")); hold_common(A,float(y1),float(d1),float(yaw1),5,label="terminal_hover")
        eq=["u=(x+A)/(2A)","ydot=(dy/du)xdot/(2A)","zdot=[z_C0'(x)-dd/du/(2A)]xdot","yaw_rate=(dyaw/du)xdot/(2A)",f"profile={fid}"]
    elif fid in {"CAL12","VAL04","TEST04"}:
        name={"CAL12":"fov_visibility_sweep","VAL04":"independent_reentry","TEST04":"hard_visibility_recovery"}[fid]
        x=0 if fid!="VAL04" else .5*A; d=4 if fid!="VAL04" else 5
        points=[0,8,0] if fid=="VAL04" else [0,8,0,-8,0]; speed=1.25 if fid=="VAL04" else 1.
        hold_common(x,0,d,duration=5)
        current_yaw=0.
        for i,(y0,y1) in enumerate(zip(points,points[1:])):
            target_yaw=(15*rad if fid=="VAL04" and y1==0 else (15*rad if fid=="TEST04" and y0>0 and y1==0 else (-15*rad if fid=="TEST04" and y0<0 and y1==0 else 0)))
            de=float(executed_clearance(d)); p0=np.array([x,y0,g.z(c0,x)-de]); p1=np.array([x,y1,g.z(c0,x)-de]); duration=1.875*abs(y1-y0)/speed
            b.transition((*p0,current_yaw),(*p1,target_yaw),duration,f"lateral_leg_{i+1}"); current_yaw=target_yaw
            if abs(y1)==8: b.hold(x,y1,d,current_yaw,8 if fid=="VAL04" else (10 if fid=="TEST04" else 5),f"outside_dwell_{i+1}")
            if y1==0 and i < len(points)-2 and fid in {"CAL12","TEST04"}:
                b.hold(x,0,d,current_yaw,5,f"midpoint_settle_{i+1}")
        if current_yaw:
            de=float(executed_clearance(d)); b.transition((x,0,g.z(c0,x)-de,current_yaw),(x,0,g.z(c0,x)-de,0),3,"yaw_settle")
        hold_common(x,0,d,duration=5,label="terminal_hover")
        eq=[f"lateral points={points}; quintic transitions at nominal {speed} m/s","v=(p1-p0)s'(t/T)/T","CAL12/TEST04 include a documented 5 s zero-velocity midpoint settle between opposing excursions","return yaw convention: positive after +y excursion; negative after -y excursion"]
    elif fid=="TEST01":
        name="approach_like"; x0=-.5*A; x1=.5*A
        def shape(x,xd):
            u=(x-x0)/(x1-x0); s=smoother(u); sd=smoother_d(u)/(x1-x0)
            y=4-4*s; d=8-5.5*s; yaw=(10*rad)*(1-s); de=executed_clearance(d)
            dde_dx=executed_clearance_derivative(-5.5*sd)
            return np.array([x,y,g.z(c0,x)-de]),np.array([xd,-4*sd*xd,(g.slope(c0,x)-dde_dx)*xd]),yaw,-10*rad*sd*xd
        hold_common(x0,4,8,10*rad,5); adaptations.append(b.traverse(x0,x1,1,shape,"approach")); hold_common(x1,0,2.5,0,5,label="terminal_hover")
        eq=["u=(x+0.5A)/A; y=4[1-s(u)]; d=8-5.5s(u); yaw=10deg[1-s(u)]","derivatives use s'(u)du/dt and zdot=z_C0'(x)xdot-ddot"]
    elif fid=="TEST03":
        name="speed_sweep"; speeds=[.5,1,2,3,5]; bounds=np.linspace(-A,A,6); L=bounds[1]-bounds[0]; hold_common(-A,-1,4,duration=5); shape=constant_shape(-1,4)
        x=-A; x=b.velocity_transition_distance(x,.15*L,0,.5,shape,"entry_ramp")
        for i,speed in enumerate(speeds[:-1]):
            plateau_fraction=.45 if i==3 else .70
            transition_fraction=.55 if i==3 else .30
            x=b.constant_velocity_distance(x,plateau_fraction*L,speed,shape,f"speed_plateau_{i+1}")
            x=b.velocity_transition_distance(x,transition_fraction*L,speed,speeds[i+1],shape,f"speed_transition_{i+1}_{i+2}")
        x=b.constant_velocity_distance(x,.05*L,5,shape,"speed_plateau_5")
        x=b.velocity_transition_distance(x,.80*L,5,0,shape,"terminal_deceleration")
        hold_common(A,-1,4,duration=5,label="terminal_hover")
        adaptations.append("TEST03 5 m/s plateau shortened to 5% of section 5; terminal deceleration uses its remaining 80% to satisfy the fixed span and terminal hover")
        eq=["five equal-distance sections; targets [0.5,1,2,3,5] m/s","v=v0+(v1-v0)s(t/T), with exact analytic position integral; sections 1-4 have 70% plateaus"]
    elif fid=="CANARY":
        name="recorder_interface_canary"; x0=-.4*A; x1=.4*A
        def shape(x,xd):
            u=(x-x0)/(x1-x0); y=.75*np.sin(2*np.pi*u); d=4.5; yaw=6*rad*np.sin(2*np.pi*u)
            dydu=1.5*np.pi*np.cos(2*np.pi*u); dyawdu=12*np.pi*rad*np.cos(2*np.pi*u); ud=xd/(x1-x0)
            return np.array([x,y,g.z(c0,x)-executed_clearance(d)]),np.array([xd,dydu*ud,g.slope(c0,x)*xd]),yaw,dyawdu*ud
        hold_common(x0,0,4.5,0,5); adaptations.append(b.traverse(x0,x1,.8,shape,"representative_translation"));hold_common(x1,0,4.5,0,5,label="terminal_hover")
        eq=["u=(x+0.4A)/(0.8A); y=0.75sin(2piu); d_req=4.5; yaw=6sin(2piu)deg","x uses smooth acceleration/cruise/deceleration; all first derivatives use exact chain rule"]
    elif fid=="FINALTEST01":
        name="final_ordinary_mixed_generalization";x0=-.55*A;x1=.45*A
        def shape(x,xd):
            u=(x-x0)/(x1-x0);s=smoother(u);sd=smoother_d(u)/(x1-x0);y=2.5-3.25*s;d=6.5-3*s;yaw=(8-14*s)*rad
            return np.array([x,y,g.z(c0,x)-executed_clearance(d)]),np.array([xd,-3.25*sd*xd,(g.slope(c0,x)-executed_clearance_derivative(-3*sd))*xd]),yaw,-14*rad*sd*xd
        hold_common(x0,2.5,6.5,8*rad,5);adaptations.append(b.traverse(x0,x1,1.4,shape,"mixed_approach"));hold_common(x1,-.75,3.5,-6*rad,5,label="terminal_hover");eq=["u=(x+0.55A)/A; y=2.5-3.25s(u); d_req=6.5-3s(u); yaw=(8-14s(u))deg","exact first derivatives by chain rule"]
    elif fid=="FINALTEST02":
        name="final_aggressive_combined";speed=2.2
        def values(u):return 2.25*np.sin(2.5*np.pi*u),4.75+1.4*np.sin(3*np.pi*u+np.pi/6),18*rad*np.sin(2.5*np.pi*u+np.pi/8),5.625*np.pi*np.cos(2.5*np.pi*u),4.2*np.pi*np.cos(3*np.pi*u+np.pi/6),45*np.pi*rad*np.cos(2.5*np.pi*u+np.pi/8)
        def shape(x,xd):
            u=(x+A)/(2*A);y,d,yaw,dydu,dddu,dyawdu=values(u);ud=xd/(2*A);return np.array([x,y,g.z(c0,x)-executed_clearance(d)]),np.array([xd,dydu*ud,(g.slope(c0,x)-executed_clearance_derivative(dddu)/(2*A))*xd]),yaw,dyawdu*ud
        y0,d0,yaw0,*_=values(0);y1,d1,yaw1,*_=values(1);hold_common(-A,float(y0),float(d0),float(yaw0),5);adaptations.append(b.traverse(-A,A,speed,shape,"aggressive_combined"));hold_common(A,float(y1),float(d1),float(yaw1),5,label="terminal_hover");scale=1.10;b.t=[t*scale for t in b.t];b.v=[v/scale for v in b.v];b.yr=[r/scale for r in b.yr];adaptations.append("uniform time scale 1.10 applied after deterministic tracking-limit validation; exact spatial path preserved");eq=["u=(x+A)/(2A); specified 5pi/2 lateral/yaw and 3pi clearance profiles","all derivatives analytic; uniform longitudinal timing preserves spatial path"]
    elif fid=="FINALTEST03":
        name="final_six_regime_speed";speeds=[1.25,3.75,.60,2.25,4.75,1.75];bounds=np.linspace(-A,A,7);L=bounds[1]-bounds[0];shape=constant_shape(-.5,4.25);hold_common(-A,-.5,4.25,duration=5);x=-A;x=b.velocity_transition_distance(x,.65*L,0,speeds[0],shape,"entry");x=b.constant_velocity_distance(x,.35*L,speeds[0],shape,"plateau_1")
        for i in range(1,6):
            tf=.98 if i<5 else .68;x=b.velocity_transition_distance(x,tf*L,speeds[i-1],speeds[i],shape,f"transition_{i}_{i+1}");x=b.constant_velocity_distance(x,.02*L,speeds[i],shape,f"plateau_{i+1}")
        x=b.velocity_transition_distance(x,.30*L,speeds[-1],0,shape,"terminal_deceleration");hold_common(A,-.5,4.25,duration=5,label="terminal_hover");scale=1.07;b.t=[t*scale for t in b.t];b.v=[v/scale for v in b.v];b.yr=[r/scale for r in b.yr];adaptations.append("uniform time scale 1.07 applied to bound peak acceleration; exact spatial path and speed-regime ratios/order preserved");eq=["six equal-distance regimes [1.25,3.75,0.60,2.25,4.75,1.75]m/s before uniform safety time scaling","quintic velocity transitions with exact integrated position; y=-0.5; z follows C0"]
    elif fid=="FINALTEST04":
        name="final_partial_visibility_c0_return";x=.2*A;d=4.25;points=[0,-6.5,-2,6,1,0];dwells={-6.5:6,-2:3,6:6,1:3};targets=[-10*rad,-10*rad,12*rad,12*rad,0];hold_common(x,0,d,0,5);current=0
        for i,(y0,y1) in enumerate(zip(points,points[1:])):
            z=g.z(c0,x)-executed_clearance(d);b.transition((x,y0,z,current),(x,y1,z,targets[i]),1.875*abs(y1-y0)/1.1,f"visibility_leg_{i+1}");current=targets[i]
            if y1 in dwells:b.hold(x,y1,d,current,dwells[y1],f"dwell_{y1:+g}")
        hold_common(x,0,d,0,5,label="terminal_hover");scale=1.25;b.t=[t*scale for t in b.t];b.v=[v/scale for v in b.v];b.yr=[r/scale for r in b.yr];adaptations.append("uniform time scale 1.25 applied after PX4 multi-reversal execution failure; exact visibility geometry and dwell ordering preserved");eq=["x=0.2A; y=0,-6.5,-2,+6,+1,0 with specified dwells","yaw 0->-10, cross-corridor -10->+12, final +12->0; C2 transitions"]
    elif fid=="FINALTEST05":
        name="final_total_loss_long_recovery";x=0.;d=4.5;h=float(executed_clearance(d));drone_z=float(g.z(c0,x)-h);max_h=max(float(np.max(g.conductor_G(cid)[:,2]-drone_z)) for cid in g.ids);limit=max(max_h*math.tan(math.radians(40)),max_h/.7);all_y=np.concatenate([g.conductor_G(cid)[:,1] for cid in g.ids]);selected=next(float(y) for y in np.arange(8.,30.01,.25) if float(np.min(np.abs(y-all_y)))>limit+2);z=drone_z;hold_common(x,0,d,0,5);b.transition_limited((x,0,z,0),(x,selected,z,0),1.,"departure_to_loss");b.hold(x,selected,d,0,35,"verified_total_loss_dwell");b.transition_limited((x,selected,z,0),(x,0,z,0),1.,"robust_reentry");b.hold(x,0,d,0,30,"post_reentry_visible_window");adaptations.append(f"exact FOV search selected y={selected:.2f}m with >2m cone-boundary margin; 35s loss dwell and 30s visible recovery");eq=["exact sampled geometry/FOV search; y 0->loss->0 via C2 quintics","35s loss dwell; 30s centered visible post-reentry hover"]
    elif fid=="POSTTEST01":
        name="postfix_approach_like"; x0=-.65*A; x1=.35*A
        def shape(x,xd):
            u=(x-x0)/(x1-x0); s=smoother(u); sd=smoother_d(u)/(x1-x0)
            y=-3.5+4*s; d=7-4*s; yaw=(-12+16*s)*rad
            return np.array([x,y,g.z(c0,x)-executed_clearance(d)]),np.array([xd,4*sd*xd,(g.slope(c0,x)-executed_clearance_derivative(-4*sd))*xd]),yaw,16*rad*sd*xd
        hold_common(x0,-3.5,7,-12*rad,5); adaptations.append(b.traverse(x0,x1,1.25,shape,"approach")); hold_common(x1,.5,3,4*rad,5,label="terminal_hover")
        eq=["u=(x+0.65A)/A; y=-3.5+4s(u); d_req=7-4s(u); yaw=(-12+16s(u))deg","derivatives use exact s'(u)du/dt; z=z_C0(x)-d_exec(d_req)"]
    elif fid=="POSTTEST02":
        name="postfix_aggressive_combined"; speed=2.0
        def values(u): return 2*np.sin(3*np.pi*u),4.5+1.5*np.sin(2*np.pi*u+np.pi/4),17*rad*np.sin(3*np.pi*u),6*np.pi*np.cos(3*np.pi*u),3*np.pi*np.cos(2*np.pi*u+np.pi/4),51*np.pi*rad*np.cos(3*np.pi*u)
        def shape(x,xd):
            u=(x+A)/(2*A); y,d,yaw,dydu,dddu,dyawdu=values(u); ud=xd/(2*A)
            return np.array([x,y,g.z(c0,x)-executed_clearance(d)]),np.array([xd,dydu*ud,(g.slope(c0,x)-executed_clearance_derivative(dddu)/(2*A))*xd]),yaw,dyawdu*ud
        y0,d0,yaw0,*_=values(0); y1,d1,yaw1,*_=values(1); hold_common(-A,float(y0),float(d0),float(yaw0),5)
        adaptations.append(b.traverse(-A,A,speed,shape,"combined_traversal")); hold_common(A,float(y1),float(d1),float(yaw1),5,label="terminal_hover")
        eq=["u=(x+A)/(2A); y=2sin(3piu); d_req=4.5+1.5sin(2piu+pi/4); yaw=17sin(3piu)deg","all first derivatives analytic by chain rule; longitudinal profile has quintic ramps"]
    elif fid=="POSTTEST03":
        name="postfix_speed_generalization"; speeds=[.75,1.75,3.5,1.25,4.5,2.5]; bounds=np.linspace(-A,A,7); L=bounds[1]-bounds[0]; shape=constant_shape(.75,4.5); hold_common(-A,.75,4.5,duration=5); x=-A
        x=b.velocity_transition_distance(x,.60*L,0,speeds[0],shape,"entry_ramp"); x=b.constant_velocity_distance(x,.40*L,speeds[0],shape,"plateau_1")
        for i in range(1,6):
            transition_fraction=.60 if i==5 else .80
            x=b.velocity_transition_distance(x,transition_fraction*L,speeds[i-1],speeds[i],shape,f"transition_{i}_{i+1}")
            frac=.10 if i==5 else .20
            x=b.constant_velocity_distance(x,frac*L,speeds[i],shape,f"plateau_{i+1}")
        x=b.velocity_transition_distance(x,.30*L,speeds[-1],0,shape,"terminal_deceleration"); hold_common(A,.75,4.5,duration=5,label="terminal_hover")
        eq=["six equal-distance sections with plateaus [0.75,1.75,3.5,1.25,4.5,2.5] m/s","transitions use v=v0+(v1-v0)s(t/T) and exact integral x(t); xddot=(v1-v0)s'(t/T)/T","y=.75; z=z_C0(x)-d_exec(4.5); ydot=0; zdot=z_C0'(x)xdot"]
    elif fid=="POSTTEST04":
        name="postfix_hard_visibility_reacquisition"; x=-.25*A; d=4.5; points=[0,7,2,-7,0]; dwells={7:7,2:3,-7:7}; yaws=[0,12*rad,-12*rad,0]; hold_common(x,0,d,0,5); current_yaw=0
        for i,(y0,y1) in enumerate(zip(points,points[1:])):
            target=yaws[i]; z=g.z(c0,x)-executed_clearance(d); duration=1.875*abs(y1-y0)/1.1
            b.transition((x,y0,z,current_yaw),(x,y1,z,target),duration,f"visibility_leg_{i+1}"); current_yaw=target
            if y1 in dwells:b.hold(x,y1,d,current_yaw,dwells[y1],f"dwell_y_{y1:+g}")
        hold_common(x,0,d,0,5,label="terminal_hover")
        eq=["x=-0.25A; y points 0,+7,+2,-7,0 with quintic transitions at nominal 1.1m/s","yaw endpoints 0,+12,-12,0deg use same C2 transition; yaw_rate analytic"]
    elif fid=="POSTTEST05":
        name="postfix_all_conductor_loss"; x=0.; d=4.5
        # Exact sensor models imply absence when vertical sensor-axis range h
        # fails radar h > 0.7*lateral and camera |lateral| < h*tan(40deg).
        # Dense geometry search selects the smallest integer +y with >=2 m
        # lateral margin to both conductor envelopes and no pylon proximity.
        candidates=np.arange(8.,30.01,.25); selected=None; proof={}
        h=float(executed_clearance(d)); drone_z=float(g.z(c0,x)-h)
        maximum_vertical_separation=max(float(np.max(g.conductor_G(cid)[:,2]-drone_z)) for cid in g.ids)
        camera_limit=maximum_vertical_separation*math.tan(math.radians(40)); radar_limit=maximum_vertical_separation/0.7
        conductor_y=np.concatenate([g.conductor_G(cid)[:,1] for cid in g.ids])
        for y in candidates:
            lateral=float(np.min(np.abs(y-conductor_y)))
            if lateral>max(camera_limit,radar_limit)+2.0: selected=float(y); proof={"camera_lateral_limit_m":camera_limit,"radar_lateral_limit_m":radar_limit,"nearest_conductor_lateral_distance_m":lateral,"robust_margin_m":lateral-max(camera_limit,radar_limit)}; break
        if selected is None: raise RuntimeError("no safe all-conductor-loss pose")
        z=g.z(c0,x)-h; hold_common(x,0,d,0,5); b.transition_limited((x,0,z,0),(x,selected,z,0),1.0,"outbound_to_loss"); b.hold(x,selected,d,0,10,"all_conductor_loss_dwell"); b.transition_limited((x,selected,z,0),(x,0,z,0),1.0,"return_to_corridor"); b.hold(x,0,d,0,10,"terminal_visible_hover")
        adaptations.append("preflight exact-FOV search selected y=%.2f m; proof=%s"%(selected,json.dumps(proof,sort_keys=True)))
        eq=["x=0; d_req=4.5; y:0->y_loss->0 via quintic minimum-jerk transitions","y_loss is the first 0.25m-grid safe pose satisfying camera and radar absence with >=2m angular/lateral margin","10s loss dwell and 10s terminal visible hover; v and yaw-rate are analytic"]
    else: raise KeyError(fid)
    return b.result(spec,name,eq,adaptations)


def nearest_curve_distance(g: Corridor, p: np.ndarray):
    curves=np.concatenate([np.column_stack((g.conductor_G(cid),np.full(1001,i))) for i,cid in enumerate(g.ids)])
    # chunk to control memory
    minimum=np.full(len(p),np.inf); nearest=np.full(len(p),-1)
    for chunk in np.array_split(np.arange(len(p)),max(1,len(p)//1000)):
        d=np.linalg.norm(p[chunk,None,:]-curves[None,:,:3],axis=2); ix=np.argmin(d,axis=1)
        minimum[chunk]=d[np.arange(len(chunk)),ix]; nearest[chunk]=curves[ix,3].astype(int)
    return minimum,nearest


def validate(tr: Trajectory,g: Corridor):
    dt=np.diff(tr.t); dv=np.diff(tr.v,axis=0); accel=np.linalg.norm(dv/dt[:,None],axis=1)
    speed=np.linalg.norm(tr.v,axis=1); dyaw=np.unwrap(tr.yaw); yaw_acc=np.diff(tr.yaw_rate)/dt
    world=g.world(tr.p[:,0],tr.p[:,1],tr.p[:,2]); distances,nearest=nearest_curve_distance(g,tr.p)
    c0_clear=g.z(g.ids[0],tr.p[:,0])-tr.p[:,2]
    checks={
      "finite":bool(np.isfinite(np.column_stack((tr.t,tr.p,tr.v,tr.yaw,tr.yaw_rate))).all()),
      "strictly_monotonic_time":bool((dt>0).all()),
      "inside_usable_span":bool((tr.p[:,0]>=g.x_limits[0]-1e-6).all() and (tr.p[:,0]<=g.x_limits[1]+1e-6).all()),
      "minimum_conductor_clearance_m":float(distances.min()),
      "conductor_clearance_pass":bool(distances.min()>=0.49),
      "minimum_ground_clearance_m":float(world[:,2].min()),
      "ground_clearance_pass":bool(world[:,2].min()>=0.5),
      "c0_clearance_identity_max_error_m":float(np.max(np.abs(c0_clear-(g.z(g.ids[0],tr.p[:,0])-tr.p[:,2])))),
      "velocity_transition_max_jump_mps":float(np.linalg.norm(dv,axis=1).max(initial=0)),
      "velocity_continuity_pass":bool(np.linalg.norm(dv,axis=1).max(initial=0)<0.18),
      "max_speed_mps":float(speed.max()),
      "max_acceleration_mps2":float(accel.max(initial=0)),
      "acceleration_pass":bool(accel.max(initial=0)<=8.0),
      "yaw_rate_max_rps":float(np.abs(tr.yaw_rate).max()),
      "yaw_rate_transition_max_jump_rps":float(np.abs(np.diff(tr.yaw_rate)).max(initial=0)),
      "yaw_continuity_pass":bool(np.abs(np.diff(dyaw)).max(initial=0)<0.05 and np.abs(np.diff(tr.yaw_rate)).max(initial=0)<0.15),
      "start_end_zero_velocity":bool(np.linalg.norm(tr.v[[0,-1]],axis=1).max()<1e-9),
    }
    mandatory=[k for k in checks if k.endswith("_pass")]+["finite","strictly_monotonic_time","inside_usable_span","start_end_zero_velocity"]
    return {"status":"passed" if all(checks[k] for k in mandatory) else "failed","checks":checks,"mandatory_checks":mandatory,
      "nearest_conductor_sample_counts":{g.ids[i]:int((nearest==i).sum()) for i in range(len(g.ids))}}


def write_flight(root: Path,tr:Trajectory,g:Corridor):
    d=root/tr.split/tr.flight_id; (d/"bag").mkdir(parents=True,exist_ok=True); (d/"quicklook").mkdir(exist_ok=True)
    clearance_adaptation={"reason":"unchanged world cannot safely accommodate requested clearance below bottom C0","formula":"d_executed = 0.5 + (d_requested - 2.5) / 5.5","requested_domain_m":[2.5,8.0],"executed_domain_m":[0.5,1.5],"approved_by_user":True}
    definition={"flight_id":tr.flight_id,"split":tr.split,"seed":tr.seed,"name":tr.name,"corridor_frame":"G","A_m":g.A,"equations":tr.equations,"global_clearance_safety_adaptation":clearance_adaptation,"safety_adaptations":tr.safety_adaptations,"setpoint_rate_hz":SETPOINT_HZ,"duration_s":float(tr.t[-1])}
    (d/"trajectory_definition.json").write_text(json.dumps(definition,indent=2)+"\n")
    with (d/"trajectory_samples.csv").open("w",newline="") as f:
        w=csv.writer(f); w.writerow(["t_s","x_G_m","y_G_m","z_G_m","vx_G_mps","vy_G_mps","vz_G_mps","yaw_rad","yaw_rate_rps","phase"])
        for row,phase in zip(np.column_stack((tr.t,tr.p,tr.v,tr.yaw,tr.yaw_rate)),tr.phase): w.writerow([*map(lambda x:f"{x:.9f}",row),phase])
    report=validate(tr,g); (d/"validation_report.json").write_text(json.dumps(report,indent=2)+"\n")
    metadata={**definition,"status":"preflight_validated" if report["status"]=="passed" else "preflight_failed","required_topics":RECORD_TOPICS}
    (d/"flight_metadata.json").write_text(json.dumps(metadata,indent=2)+"\n")
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    fig,axes=plt.subplots(1,2,figsize=(12,5),dpi=140)
    for cid in g.ids:
        c=g.conductor_G(cid); axes[0].plot(c[:,0],c[:,1],label=cid); axes[1].plot(c[:,0],c[:,2],label=cid)
    axes[0].plot(tr.p[:,0],tr.p[:,1],"k",lw=2,label=tr.flight_id); axes[1].plot(tr.p[:,0],tr.p[:,2],"k",lw=2,label=tr.flight_id)
    for ax in axes: ax.axvline(-g.A,ls="--",c="gray"); ax.axvline(0,ls=":",c="gray"); ax.axvline(g.A,ls="--",c="gray"); ax.grid(); ax.legend(fontsize=7)
    axes[0].set(xlabel="x_G [m]",ylabel="y_G [m]",title="Top view"); axes[1].set(xlabel="x_G [m]",ylabel="z_G [m]",title="Side view")
    fig.tight_layout(); fig.savefig(d/"quicklook/preflight_trajectory.png"); plt.close(fig)
    return d,report


def generate(root:Path):
    g=Corridor.load(); results=[]
    for spec in FLIGHTS:
        tr=build_flight(spec,g); d,report=write_flight(root,tr,g); results.append((tr,d,report))
    corridor={"source":str(GEOMETRY),"world_frame":"world (Gazebo ENU, +z gravity-up)","C0":g.ids[0],"logical_to_asset_id":{f"C{i}":v for i,v in enumerate(g.ids)},"physical_numeric_id_mapping":{int(v.rsplit('_',1)[1]):v for v in g.ids},"origin_world_xy_m":g.origin_xy.tolist(),"x_G_world_xy":g.x_axis.tolist(),"y_G_world_xy":g.y_axis.tolist(),"z_G_world":[0,0,1],"C0_zero_slope_error":float(g.slope(g.ids[0],0)),"usable_C0_x_limits_m":g.x_limits,"A_m":g.A,"coefficients_z_of_x":{k:v.tolist() for k,v in g.coefficients.items()}}
    root.mkdir(parents=True,exist_ok=True); (root/"corridor_frame.json").write_text(json.dumps(corridor,indent=2)+"\n")
    manifest={"schema_version":1,"status":"offline_validated" if all(r[2]["status"]=="passed" for r in results) else "preflight_failed","flight_count":len(results),"split_counts":{"calibration":12,"validation":4,"test":4},"corridor":corridor,"flights":[{"flight_id":t.flight_id,"split":t.split,"seed":t.seed,"path":str(d.relative_to(root)),"duration_s":float(t.t[-1]),"preflight_status":r["status"],"safety_adaptations":t.safety_adaptations} for t,d,r in results]}
    (root/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    # Combined dataset plot.
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    fig,axes=plt.subplots(1,2,figsize=(14,6),dpi=160)
    for cid in g.ids:
        c=g.conductor_G(cid); axes[0].plot(c[:,0],c[:,1],"k",alpha=.35); axes[1].plot(c[:,0],c[:,2],"k",alpha=.35)
    for t,_,_ in results: axes[0].plot(t.p[:,0],t.p[:,1],label=t.flight_id,lw=1); axes[1].plot(t.p[:,0],t.p[:,2],label=t.flight_id,lw=1)
    for ax in axes: ax.grid(); ax.axvline(-g.A,ls="--",c="gray"); ax.axvline(g.A,ls="--",c="gray")
    axes[0].set(xlabel="x_G [m]",ylabel="y_G [m]",title="All 20 trajectories: top view"); axes[1].set(xlabel="x_G [m]",ylabel="z_G [m]",title="All 20 trajectories: side view"); axes[1].legend(ncol=2,fontsize=6)
    fig.tight_layout(); fig.savefig(root/"all_20_truth_trajectories.png"); plt.close(fig)
    return manifest


def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["generate","execute"]); p.add_argument("--output-root",type=Path,default=OUTPUT); p.add_argument("--flight-id"); args=p.parse_args()
    if args.command=="generate":
        manifest=generate(args.output_root); print(json.dumps({"status":manifest["status"],"A_m":manifest["corridor"]["A_m"],"flight_count":manifest["flight_count"]},indent=2)); return 0 if manifest["status"]=="offline_validated" else 2
    raise SystemExit("live execution is supplied by powerline_qualification_executor.py")


if __name__=="__main__": raise SystemExit(main())
