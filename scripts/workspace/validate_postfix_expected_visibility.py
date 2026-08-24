#!/usr/bin/env python3
"""Exact geometry/FOV preflight visibility evidence for post-fix flights."""
from __future__ import annotations
import json,math,sys
import argparse
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
sys.path.insert(0,str(Path(__file__).resolve().parent));import powerline_qualification as q

HFOV=1.3962634; VFOV=2*math.atan(math.tan(HFOV/2)*480/640); RADAR_RANGE=18.; RADAR_SLOPE=.7

def counts(tr,g):
    curves={cid:g.conductor_G(cid,2001) for cid in g.ids}; cam=[];rad=[]
    for p,yaw in zip(tr.p,tr.yaw):
        # Sensor optical/radar +X is gravity-up. Horizontal sensor axes are
        # body/corridor lateral and negative body/corridor-forward.
        cy,sy=math.cos(yaw),math.sin(yaw); cids=[];rids=[]
        for cid,c in curves.items():
            d=c-p; sx=d[:,2]; body_f=cy*d[:,0]+sy*d[:,1]; body_l=-sy*d[:,0]+cy*d[:,1]; sy_s=body_l; sz_s=-body_f
            in_cam=(sx>.02)&(np.abs(np.arctan2(sy_s,sx))<HFOV/2)&(np.abs(np.arctan2(sz_s,sx))<VFOV/2)
            rng=np.sqrt(sx*sx+sy_s*sy_s+sz_s*sz_s);in_rad=(rng<=RADAR_RANGE)&(sx>RADAR_SLOPE*np.hypot(sy_s,sz_s))
            if in_cam.any():cids.append(cid)
            if in_rad.any():rids.append(cid)
        cam.append(cids);rad.append(rids)
    return cam,rad

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--final',action='store_true');args=ap.parse_args();root=q.FINAL_TEST_OUTPUT if args.final else q.POSTFIX_OUTPUT;specs=q.FINAL_FLIGHTS if args.final else q.POST_FLIGHTS;g=q.Corridor.load()
    for spec in specs:
        tr=q.build_flight(spec,g,dt=1/q.DENSE_HZ);cam,rad=counts(tr,g);cc=np.array(list(map(len,cam)));rr=np.array(list(map(len,rad)));both=(cc==0)&(rr==0);dt=1/q.DENSE_HZ
        runs=[];start=None
        for i,v in enumerate(np.r_[both,False]):
            if v and start is None:start=i
            if not v and start is not None:runs.append((start,i));start=None
        report={'camera_fov_rad':{'horizontal':HFOV,'vertical':VFOV},'radar':{'range_m':RADAR_RANGE,'view_cone_slope':RADAR_SLOPE},'dense_rate_hz':q.DENSE_HZ,
          'expected_camera_count_range':[int(cc.min()),int(cc.max())],'expected_radar_count_range':[int(rr.min()),int(rr.max())],
          'longest_simultaneous_all_conductor_absence_s':max([0.]+[(b-a)*dt for a,b in runs]),'simultaneous_absence_intervals_s':[[float(tr.t[a]),float(tr.t[min(b,len(tr.t)-1)])] for a,b in runs],
          'status':'passed' if not spec['flight_id'].endswith('05') or max([0.]+[(b-a)*dt for a,b in runs])>=(34.5 if args.final else 9.5) else 'failed'}
        out=root/spec['flight_id'];(out/'preflight_visibility_validation.json').write_text(json.dumps(report,indent=2)+'\n')
        fig,ax=plt.subplots(figsize=(10,4));ax.step(tr.t,cc,label='expected camera IDs');ax.step(tr.t,rr,label='expected radar IDs');ax.fill_between(tr.t,0,4,where=both,alpha=.2,label='simultaneous absence');ax.set(xlabel='t [s]',ylabel='physical conductor count',ylim=(-.1,4.2),title=spec['flight_id']+' exact geometry/FOV preflight');ax.grid();ax.legend();fig.tight_layout();fig.savefig(out/'quicklook'/'preflight_visibility.png',dpi=150);plt.close(fig)
        if report['status']!='passed':raise SystemExit(f"{spec['flight_id']} visibility failed: {report}")
        print(spec['flight_id'],report['expected_camera_count_range'],report['expected_radar_count_range'],report['longest_simultaneous_all_conductor_absence_s'])
if __name__=='__main__':main()
