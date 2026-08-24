#!/usr/bin/env python3
"""Validate one completed qualification bag and its executed truth trajectory."""
from __future__ import annotations
import argparse,csv,json,math,sys
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt

sys.path.insert(0,str(Path(__file__).resolve().parent))
import powerline_qualification as catalog
from inspect_perception_ground_truth import inspect_bag

def main():
    ap=argparse.ArgumentParser();ap.add_argument('flight_id');ap.add_argument('--output-root',type=Path,default=catalog.OUTPUT);a=ap.parse_args()
    spec=next(x for x in catalog.ALL_FLIGHTS if x['flight_id']==a.flight_id); out=a.output_root/spec['split']/a.flight_id
    topic=inspect_bag((out/'bag').resolve())
    rows=list(csv.DictReader((out/'actual_trajectory.csv').open()))
    def arr(names): return np.array([[float(r[n]) for n in names] for r in rows])
    cp=arr(['cmd_xG','cmd_yG','cmd_zG']); ap_=arr(['actual_xG','actual_yG','actual_zG']); cv=arr(['cmd_vxG','cmd_vyG','cmd_vzG']); av=arr(['actual_vxG','actual_vyG','actual_vzG'])
    pe=np.linalg.norm(ap_-cp,axis=1); ve=np.linalg.norm(av-cv,axis=1)
    cy=arr(['cmd_yaw'])[:,0]; ay=arr(['actual_yawG'])[:,0]; ye=np.abs(np.arctan2(np.sin(ay-cy),np.cos(ay-cy)))
    metrics={'position_rms_m':float(np.sqrt(np.mean(pe**2))),'position_max_m':float(pe.max()),'velocity_rms_mps':float(np.sqrt(np.mean(ve**2))),'velocity_max_mps':float(ve.max()),'yaw_rms_deg':float(np.degrees(np.sqrt(np.mean(ye**2)))),'yaw_max_deg':float(np.degrees(ye.max()))}
    commanded_peak_speed=float(np.linalg.norm(cv,axis=1).max())
    # Combined trajectories can have a total vector speed substantially above
    # their longitudinal cruise speed.  Scale both RMS tracking gates from the
    # commanded vector-speed envelope; this is a validation-policy correction
    # only and never changes a command, controller setting, or recording.
    tolerances={'position_rms_m':max(0.75,0.30*commanded_peak_speed),'position_max_m':max(2.0,0.70*commanded_peak_speed),'velocity_rms_mps':max(1.25,0.40*commanded_peak_speed),'velocity_max_mps':max(3.0,commanded_peak_speed),'yaw_rms_deg':20.0,'yaw_max_deg':45.0}
    tracking=all(metrics[k]<=v for k,v in tolerances.items()) and all(int(r['nav_state'])==14 for r in rows)
    report={'flight_id':a.flight_id,'success':bool(topic['success'] and tracking),'topic_ground_truth':topic,'tracking':{'success':tracking,'metrics':metrics,'tolerances':tolerances,'sample_count':len(rows)}}
    (out/'topic_validation.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    fig,axs=plt.subplots(1,2,figsize=(12,5)); axs[0].plot(cp[:,0],cp[:,1],'k--',label='command');axs[0].plot(ap_[:,0],ap_[:,1],label='sim GT');axs[0].set(xlabel='x_G [m]',ylabel='y_G [m]',title=f'{a.flight_id} plan view');axs[0].axis('equal');axs[0].grid();axs[0].legend()
    g=catalog.Corridor.load();xx=np.linspace(-g.A,g.A,300)
    for conductor_id in g.ids: axs[1].plot(xx,[g.z(conductor_id,x) for x in xx],label=conductor_id)
    axs[1].plot(cp[:,0],cp[:,2],'k--',label='command');axs[1].plot(ap_[:,0],ap_[:,2],label='sim GT');axs[1].set(xlabel='x_G [m]',ylabel='world z [m]',title='side view');axs[1].grid();axs[1].legend(fontsize=7)
    fig.tight_layout();fig.savefig(out/'quicklook_executed.png',dpi=160);plt.close(fig)
    print(json.dumps({'flight_id':a.flight_id,'success':report['success'],'tracking':metrics,'camera':topic['camera'],'radar':topic['radar']},indent=2));return 0 if report['success'] else 1
if __name__=='__main__': raise SystemExit(main())
