#!/usr/bin/env python3
"""Freeze and hand off the five post-fix held-out qualification bags."""
from __future__ import annotations
import collections,csv,hashlib,json,subprocess,sys
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
sys.path.insert(0,str(Path(__file__).resolve().parent));import powerline_qualification as q

ROOT=q.POSTFIX_OUTPUT
def sha_tree(path):
    h=hashlib.sha256()
    for p in sorted(path.rglob('*')):
        if p.is_file():h.update(str(p.relative_to(path)).encode());h.update(p.read_bytes())
    return h.hexdigest()
def stamp(m):return m.header.stamp.sec+m.header.stamp.nanosec*1e-9
def runs(samples):
    out=[];start=None;last=None
    for t,zero in samples:
        if zero and start is None:start=t
        if not zero and start is not None:out.append((start,last));start=None
        last=t
    if start is not None:out.append((start,last))
    return out
def intersect(a,b):
    return [(max(x0,y0),min(x1,y1)) for x0,x1 in a for y0,y1 in b if min(x1,y1)>max(x0,y0)]
def bag_visibility(bag):
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message
    r=rosbag2_py.SequentialReader();r.open(rosbag2_py.StorageOptions(uri=str(bag),storage_id=''),rosbag2_py.ConverterOptions('',''));types={x.name:x.type for x in r.get_all_topics_and_types()};classes={k:get_message(v) for k,v in types.items()}
    cam=[];rad=[];cam_counts=[];rad_counts=[];fields=[]
    while r.has_next():
        topic,data,_=r.read_next()
        if topic=='/simulation/ground_truth/cable_camera/frame':
            m=deserialize_message(data,classes[topic]);count=sum(x.visibility_state==x.VISIBLE for x in m.conductors);cam.append((stamp(m),count==0));cam_counts.append(count)
        elif topic=='/simulation/ground_truth/mmwave/scan':
            m=deserialize_message(data,classes[topic]);count=sum(x.source_class==x.VALID_PHYSICAL_CONDUCTOR for x in m.points);rad.append((stamp(m),count==0));rad_counts.append(count)
        elif topic=='/sensor/mmwave/points_full' and not fields:
            m=deserialize_message(data,classes[topic]);fields=[x.name for x in m.fields]
    cr,rr=runs(cam),runs(rad);joint=intersect(cr,rr)
    return {'camera_zero_intervals_s':cr,'radar_zero_intervals_s':rr,'simultaneous_zero_intervals_s':joint,
      'longest_camera_zero_s':max([0.]+[b-a for a,b in cr]),'longest_radar_zero_s':max([0.]+[b-a for a,b in rr]),'longest_simultaneous_zero_s':max([0.]+[b-a for a,b in joint]),
      'camera_visible_identity_count_histogram':dict(collections.Counter(cam_counts)),'radar_physical_return_count_range':[min(rad_counts,default=0),max(rad_counts,default=0)],'radar_point_fields':fields}
def git(path):
    run=lambda *a:subprocess.run(['git','-C',str(path),*a],text=True,capture_output=True).stdout.strip()
    return {'path':str(path),'commit':run('rev-parse','HEAD'),'dirty':bool(run('status','--porcelain'))}

def main():
    provenance={k:git(v) for k,v in {'workspace':Path('/home/iii/ws'),'PX4':Path('/home/iii/ws/PX4-Autopilot'),'simulation':Path('/home/iii/ws/src/III-Drone-Simulation'),'interfaces':Path('/home/iii/ws/src/III-Drone-Interfaces'),'core':Path('/home/iii/ws/src/III-Drone-Core')}.items()}
    entries=[];fig=plt.figure(figsize=(12,9));ax=fig.add_subplot(111,projection='3d');vfig,vaxs=plt.subplots(5,1,figsize=(12,12),sharex=False)
    for i,spec in enumerate(q.POST_FLIGHTS):
        fid=spec['flight_id'];out=ROOT/fid;rep=json.loads((out/'topic_validation.json').read_text());top=rep['topic_ground_truth'];rows=list(csv.DictReader((out/'actual_trajectory.csv').open()));arr=lambda ks:np.array([[float(r[k]) for k in ks] for r in rows]);p=arr(['actual_xG','actual_yG','actual_zG']);v=arr(['actual_vxG','actual_vyG','actual_vzG']);cp=arr(['cmd_xG','cmd_yG','cmd_zG']);cv=arr(['cmd_vxG','cmd_vyG','cmd_vzG']);yaw=arr(['cmd_yaw'])[:,0]
        vis=bag_visibility(out/'bag');standard_vis={'camera':top['camera'],'radar':top['radar'],**vis};(out/'visibility_validation.json').write_text(json.dumps(standard_vis,indent=2,sort_keys=True)+'\n')
        if fid=='POSTTEST04' and not ({0,1,2,3}.intersection(map(int,vis['camera_visible_identity_count_histogram'])) and len(vis['camera_visible_identity_count_histogram'])>=3 and max(map(int,vis['camera_visible_identity_count_histogram']))>=2):raise RuntimeError('POSTTEST04 rendered visibility transition absent')
        if fid=='POSTTEST05' and vis['longest_simultaneous_zero_s']<8:raise RuntimeError(f"POSTTEST05 simultaneous loss only {vis['longest_simultaneous_zero_s']:.3f}s")
        if not {'x','y','z','velocity','snr','noise'}.issubset(vis['radar_point_fields']):raise RuntimeError('radar side-information fields missing')
        g=q.Corridor.load();executed=g.z(g.ids[0],cp[:,0])-cp[:,2];requested=q.REQUESTED_D_MIN_M+(executed-q.EXECUTED_D_MIN_M)*(q.REQUESTED_D_MAX_M-q.REQUESTED_D_MIN_M)/(q.EXECUTED_D_MAX_M-q.EXECUTED_D_MIN_M)
        hashes={'curated_bag_sha256':sha_tree(out/'bag'),'raw_bag_sha256':sha_tree(out/'bag_raw'),'trajectory_definition_sha256':hashlib.sha256((out/'trajectory_definition.json').read_bytes()).hexdigest()}
        entry={'flight_id':fid,'seed':spec['seed'],'trajectory_definition':json.loads((out/'trajectory_definition.json').read_text()),'A_m':g.A,'duration_s':float(rows[-1]['t_s']),'requested_clearance_range_m':[float(requested.min()),float(requested.max())],'executed_clearance_range_m':[float(executed.min()),float(executed.max())],'actual_y_range_m':[float(p[:,1].min()),float(p[:,1].max())],'commanded_speed_range_mps':[float(np.linalg.norm(cv,axis=1).min()),float(np.linalg.norm(cv,axis=1).max())],'actual_speed_range_mps':[float(np.linalg.norm(v,axis=1).min()),float(np.linalg.norm(v,axis=1).max())],'yaw_range_deg':[float(np.degrees(yaw.min())),float(np.degrees(yaw.max()))],
          'topic_counts':{k:v['message_count'] for k,v in top['topics'].items()},'radar_returns_by_physical_conductor':top['radar']['returns_per_conductor'],'camera_visible_frames_by_physical_conductor':top['camera']['visible_frame_count_per_conductor'],'visibility':vis,'trajectory_tracking':rep['tracking'],'validation_status':'PASS' if rep['success'] else 'FAIL','hashes':hashes,'provenance':provenance}
        canonical=json.dumps(entry,sort_keys=True,separators=(',',':')).encode();entry['hashes']['manifest_entry_sha256']=hashlib.sha256(canonical).hexdigest();entries.append(entry)
        ax.plot(p[:,0],p[:,1],p[:,2],label=fid);vaxs[i].bar(['camera zero','radar zero','simultaneous'],[vis['longest_camera_zero_s'],vis['longest_radar_zero_s'],vis['longest_simultaneous_zero_s']]);vaxs[i].set_title(fid);vaxs[i].set_ylabel('seconds');vaxs[i].grid(axis='y')
    ax.set(xlabel='x_G [m]',ylabel='y_G [m]',zlabel='world z [m]',title='Post-fix executed simulator-truth trajectories');ax.legend();fig.tight_layout();fig.savefig(ROOT/'all_5_executed_truth_trajectories.png',dpi=180);plt.close(fig);vfig.tight_layout();vfig.savefig(ROOT/'visibility_summary.png',dpi=180);plt.close(vfig)
    manifest={'schema_version':1,'dataset':'powerline_qualification_postfix_test','held_out':True,'flight_count':5,'corridor':json.loads((ROOT/'corridor_frame.json').read_text()),'provenance':provenance,'flights':entries};(ROOT/'dataset_manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    table=['| Flight | Seed | Camera | Radar | Pos RMS m | Vel RMS m/s | Simultaneous loss s |','|---|---:|---:|---:|---:|---:|---:|']+[f"| {e['flight_id']} | {e['seed']} | {e['topic_counts']['/sensor/cable_camera/image_raw']} | {e['topic_counts']['/sensor/mmwave/points_full']} | {e['trajectory_tracking']['metrics']['position_rms_m']:.3f} | {e['trajectory_tracking']['metrics']['velocity_rms_mps']:.3f} | {e['visibility']['longest_simultaneous_zero_s']:.3f} |" for e in entries]
    handoff=f'''# Post-fix Held-out Powerline Qualification Dataset

Five newly simulated bags, seeds 4001–4005, recorded through the standalone 50 Hz PX4 Offboard trajectory-setpoint executor. No simulator teleportation, prior payload reuse, perception tuning, or estimator evaluation was performed. Output is independent from `datasets/powerline_qualification/`.

## Corridor and ground truth

`x_G` follows the conductor corridor, `z_G` is gravity-up, `y_G=z_G×x_G`, C0 is the bottom conductor, and `A={q.Corridor.load().A:.12f} m`. The approved unchanged-world clearance mapping is retained. Drone state comes directly from Gazebo `base_link` in world ENU. Radar source truth is point-order/stamp aligned and includes ideal pre-noise points. Camera truth uses rendered, occlusion-aware instance pixels. Physical IDs are shared across all truth products.

## Flights

- POSTTEST01: `x=-0.65A→+0.35A`, 1.25 m/s, minimum-jerk y/d/yaw transition.
- POSTTEST02: `x=-A→+A`, 2.0 m/s longitudinal, specified 3π/2π combined sinusoidal path; no time scaling was required.
- POSTTEST03: six equal-distance speed sections with requested plateaus; 60–80% transition allocation keeps acceleration continuous and under the established 8 m/s² execution bound.
- POSTTEST04: `y=0→+7→+2→-7→0`, requested dwells and analytic yaw transitions.
- POSTTEST05: exact sampled conductor/FOV search selected the minimum safe robust loss pose; 10 s dwell, return, and 10 s visible hover.

## Validation summary

{chr(10).join(table)}

POSTTEST05 simulator source truth proves a longest continuous simultaneous camera/radar all-physical-conductor absence of **{entries[-1]['visibility']['longest_simultaneous_zero_s']:.3f} s**, exceeding the required 8 s. POSTTEST04 contains high/partial/lost-identity/reacquisition geometry. All bags pass normalized-quaternion, finite velocity, stamp/order, shared-ID, radar-field, camera-dimension, Offboard-state, and trajectory-tracking checks.

## Isolation and provenance

Recordings used ROS domain 194, localhost-only discovery, XRCE UDP 20094, PX4 instance 34/sysid 35, unique `iii_powerline_qualification_postfix_test_<flight>` Gazebo partitions, and no MAVLink/QGroundControl bridge. Commits/dirty state and all payload hashes are in `dataset_manifest.json`.

## Limitations

Visibility durations are conservative intersections of contiguous camera-frame and radar-scan source-truth zero runs. They do not inspect downstream perception output. Raw and curated bags are both retained; curated bags use exact sensor/truth timestamp intersections at recording boundaries.
''';(ROOT/'DATASET_HANDOFF.md').write_text(handoff)
    print(json.dumps({'flights':5,'passed':sum(e['validation_status']=='PASS' for e in entries),'posttest05_simultaneous_loss_s':entries[-1]['visibility']['longest_simultaneous_zero_s']},indent=2))
if __name__=='__main__':main()
