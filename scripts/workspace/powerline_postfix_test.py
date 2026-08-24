#!/usr/bin/env python3
"""Generate the five isolated held-out post-fix qualification trajectories."""
from __future__ import annotations
import json,sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parent))
import powerline_qualification as base

def main():
    root=base.POSTFIX_OUTPUT; g=base.Corridor.load(); results=[]
    if (Path('/home/iii/ws/datasets/powerline_qualification') if Path('/home/iii/ws').exists() else base.OUTPUT).resolve()==root.resolve(): raise RuntimeError('postfix output aliases original dataset')
    for spec in base.POST_FLIGHTS:
        tr=base.build_flight(spec,g); d,report=base.write_flight(root,tr,g); results.append((tr,d,report))
    corridor={"source":str(base.GEOMETRY),"world_frame":"world (Gazebo ENU)","C0":g.ids[0],"logical_to_asset_id":{f"C{i}":v for i,v in enumerate(g.ids)},"origin_world_xy_m":g.origin_xy.tolist(),"x_G_world_xy":g.x_axis.tolist(),"y_G_world_xy":g.y_axis.tolist(),"A_m":g.A,"usable_C0_x_limits_m":g.x_limits}
    root.mkdir(parents=True,exist_ok=True);(root/'corridor_frame.json').write_text(json.dumps(corridor,indent=2)+'\n')
    manifest={"schema_version":1,"status":"offline_validated" if all(r[2]['status']=='passed' for r in results) else "preflight_failed","flight_count":5,"corridor":corridor,"flights":[{"flight_id":t.flight_id,"seed":t.seed,"duration_s":float(t.t[-1]),"preflight_status":r['status'],"safety_adaptations":t.safety_adaptations} for t,_,r in results]}
    (root/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    import matplotlib;matplotlib.use('Agg');import matplotlib.pyplot as plt
    fig,axs=plt.subplots(1,2,figsize=(14,6),dpi=160)
    for cid in g.ids:
        c=g.conductor_G(cid);axs[0].plot(c[:,0],c[:,1],'k',alpha=.35);axs[1].plot(c[:,0],c[:,2],'k',alpha=.35)
    for t,_,_ in results:axs[0].plot(t.p[:,0],t.p[:,1],label=t.flight_id);axs[1].plot(t.p[:,0],t.p[:,2],label=t.flight_id)
    for ax in axs:ax.grid();ax.legend(fontsize=8);ax.axvline(-g.A,ls='--',c='gray');ax.axvline(g.A,ls='--',c='gray')
    axs[0].set(xlabel='x_G [m]',ylabel='y_G [m]',title='Post-fix planned paths');axs[1].set(xlabel='x_G [m]',ylabel='world z [m]',title='C0-following height');fig.tight_layout();fig.savefig(root/'all_5_planned_truth_trajectories.png');plt.close(fig)
    print(json.dumps({"status":manifest['status'],"A_m":g.A,"flight_count":5},indent=2));return 0 if manifest['status']=='offline_validated' else 2
if __name__=='__main__':raise SystemExit(main())
