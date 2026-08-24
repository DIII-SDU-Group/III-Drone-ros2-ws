#!/usr/bin/env python3
"""Prepare/finalize the schema-v2 recorder-interface CANARY dataset."""
from __future__ import annotations
import argparse,csv,hashlib,json,subprocess,sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parent));import powerline_qualification as q

ROOT=q.FINAL_CANARY_OUTPUT; WS=q.ROOT
AUTHORITATIVE={"drone_state":"/simulation/ground_truth/drone/state","conductor_geometry":"/simulation/ground_truth/conductors/geometry","conductor_id_map":"/simulation/ground_truth/conductor_id_map","radar_truth":"/simulation/ground_truth/mmwave/scan","camera_frame_truth":"/simulation/ground_truth/cable_camera/frame","camera_instance_mask":"/simulation/ground_truth/cable_camera/conductor_instance_mask","camera_image":"/sensor/cable_camera/image_raw","radar_points_full":"/sensor/mmwave/points_full","sensor_combined":"/fmu/out/sensor_combined"}
FREEZE_FILES=[
 'scripts/workspace/powerline_qualification.py','scripts/workspace/powerline_qualification_executor.py','scripts/workspace/run_powerline_qualification_isolated.sh','scripts/workspace/synchronize_powerline_ground_truth_bag.py','scripts/workspace/inspect_perception_ground_truth.py','scripts/workspace/validate_powerline_qualification_flight.py','scripts/workspace/powerline_final_canary.py',
 'src/III-Drone-Simulation/src/mmwave_conductor_sensor_plugin.cpp','src/III-Drone-Simulation/Gazebo-simulation-assets/world_models/hcaa_pylon_setup/conductors.yaml','PX4-Autopilot/Tools/simulation/gz/models/d4s_dc_drone/model.sdf',
 'src/III-Drone-Interfaces/msg/SimulatorDroneState.msg','src/III-Drone-Interfaces/msg/StaticConductorGeometry.msg','src/III-Drone-Interfaces/msg/RadarScanGroundTruth.msg','src/III-Drone-Interfaces/msg/RadarPointSource.msg','src/III-Drone-Interfaces/msg/CameraFrameGroundTruth.msg','src/III-Drone-Interfaces/msg/CameraConductorVisibility.msg']
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def tree_hash(files):
 h=hashlib.sha256()
 for p in sorted(files):
  rel=str(p.relative_to(WS));data=p.read_bytes();h.update(len(rel).to_bytes(4,'big'));h.update(rel.encode());h.update(len(data).to_bytes(8,'big'));h.update(data)
 return h.hexdigest()
def dir_hash(path):
 h=hashlib.sha256()
 for p in sorted(path.rglob('*')):
  if p.is_file():r=str(p.relative_to(path));d=p.read_bytes();h.update(r.encode());h.update(d)
 return h.hexdigest()
def git(path):
 run=lambda *a:subprocess.run(['git','-C',str(path),*a],text=True,capture_output=True).stdout.strip()
 return {'commit':run('rev-parse','HEAD'),'dirty':bool(run('status','--porcelain'))}
def freeze():
 files=[WS/x for x in FREEZE_FILES]; binaries={'truth_publisher':WS/'build/iii_drone_simulation/libiii_drone_mmwave_conductor_sensor_plugin.so','core':WS/'install/iii_drone_core/lib/libiii_drone_lib.so'}
 return {'source_tree_sha256':tree_hash(files),'source_files':{str(p.relative_to(WS)):sha(p) for p in files},'binary_sha256':{k:sha(v) for k,v in binaries.items()},'recorder_command_sha256':sha(WS/'scripts/workspace/run_powerline_qualification_isolated.sh'),'truth_publisher_build_sha256':sha(binaries['truth_publisher']),'message_interface_sha256':tree_hash([p for p in files if 'III-Drone-Interfaces' in str(p)]),'calibration_sha256':sha(WS/'PX4-Autopilot/Tools/simulation/gz/models/d4s_dc_drone/model.sdf'),'trajectory_executor_sha256':sha(WS/'scripts/workspace/powerline_qualification_executor.py'),'provenance':{'workspace':git(WS),'PX4':git(WS/'PX4-Autopilot'),'simulation':git(WS/'src/III-Drone-Simulation'),'interfaces':git(WS/'src/III-Drone-Interfaces'),'core':git(WS/'src/III-Drone-Core')},'isolation':{'ros_domain_id':204,'xrce_udp_port':20204,'px4_instance':44,'px4_sysid':45,'gazebo_partition_prefix':'iii_powerline_final_canary','ros_discovery':'localhost','mavlink_qgroundcontrol':False}}
def sidecar(g,f):
 return {'powerline_qualification_schema_version':2,'world_frame':'world (Gazebo ENU, +z gravity-up)','logical_to_asset_id':{f'C{i}':v for i,v in enumerate(g.ids)},'authoritative_topics':AUTHORITATIVE,'calibration_references':{'camera_intrinsics':'PX4-Autopilot/Tools/simulation/gz/models/d4s_dc_drone/model.sdf#cable_camera','camera_extrinsics':'PX4-Autopilot/Tools/simulation/gz/models/d4s_dc_drone/model.sdf#cable_camera.pose','radar_extrinsics':'PX4-Autopilot/Tools/simulation/gz/models/d4s_dc_drone/model.sdf#MmwaveConductorSensorPlugin.sensor_pose'},'time_frame_conventions':{'bag_time_unit':'nanoseconds','header_time_unit':'nanoseconds','quaternion_order':'xyzw','world_convention':'Gazebo ENU','gravity_up_axis':'+z'},'endpoint_time_policy':{'score_exact_overlap_only':True,'allow_extrapolation':False,'maximum_endpoint_sliver_truth_periods':2.0,'minimum_absolute_tolerance_ns':20000000},'recorder_provenance':f,'derivation':'authoritative bag geometry and ID-map topics; optional corridor caches intentionally omitted'}
def prepare():
 g=q.Corridor.load();spec=q.CANARY_FLIGHTS[0];tr=q.build_flight(spec,g);out,rep=q.write_flight(ROOT,tr,g)
 if rep['status']!='passed':raise RuntimeError(rep)
 f=freeze();ROOT.mkdir(parents=True,exist_ok=True);(ROOT/'recorder_freeze.json').write_text(json.dumps(f,indent=2,sort_keys=True)+'\n');(ROOT/'corridor_frame.json').write_text(json.dumps(sidecar(g,f),indent=2,sort_keys=True)+'\n')
 definition=json.loads((out/'trajectory_definition.json').read_text());definition['powerline_qualification_schema_version']=2;(out/'trajectory_definition.json').write_text(json.dumps(definition,indent=2,sort_keys=True)+'\n');meta=json.loads((out/'flight_metadata.json').read_text());meta['powerline_qualification_schema_version']=2;meta['actual_applied_seeds']={'simulation':5099,'radar':5099,'camera':'Gazebo sensor seeded by simulation seed/runtime'};(out/'flight_metadata.json').write_text(json.dumps(meta,indent=2,sort_keys=True)+'\n');(out/'preflight_validation.json').write_text((out/'validation_report.json').read_text())
 manifest={'powerline_qualification_schema_version':2,'dataset':'powerline_qualification_final_canary','dataset_role':'canary','held_out':False,'flat_layout':True,'split_policy':{'unit':'whole flight'},'recorder_build':{'workspace':'/home/iii/ws','source_tree_sha256':f['source_tree_sha256'],'recorder_command_sha256':f['recorder_command_sha256'],'truth_publisher_build_sha256':f['truth_publisher_build_sha256']},'flights':[{'flight_id':'CANARY','split':'calibration','seed':5099,'duration_s':float(tr.t[-1]),'trajectory_type':tr.name,'bag_path':'CANARY/bag','trajectory_definition_sha256':sha(out/'trajectory_definition.json'),'bag_sha256':'PENDING'}]};(ROOT/'dataset_manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n');print(json.dumps({'status':'prepared','duration_s':float(tr.t[-1]),'source_tree_sha256':f['source_tree_sha256']},indent=2))
def finalize():
 out=ROOT/'CANARY';before=json.loads((ROOT/'recorder_freeze.json').read_text());after=freeze()
 if before['source_tree_sha256']!=after['source_tree_sha256'] or before['binary_sha256']!=after['binary_sha256']:raise RuntimeError('recorder freeze changed after build')
 rep=json.loads((out/'topic_validation.json').read_text());rows=list(csv.DictReader((out/'actual_trajectory.csv').open()));manifest=json.loads((ROOT/'dataset_manifest.json').read_text());record=manifest['flights'][0];record.update({'actual_duration_s':float(rows[-1]['t_s']),'bag_sha256':dir_hash(out/'bag'),'raw_bag_sha256':dir_hash(out/'bag_raw'),'topic_counts':{k:v['message_count'] for k,v in rep['topic_ground_truth']['topics'].items()},'tracking':rep['tracking'],'topic_truth_validation':rep['topic_ground_truth'],'recorder_freeze_sha256':sha(ROOT/'recorder_freeze.json')});(ROOT/'dataset_manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
 handoff=f'''# Final recorder-matched CANARY handoff\n\nSchema v2 development CANARY recorded with seed 5099 through the standalone PX4 Offboard executor. It uses the frozen recorder/truth build in `recorder_freeze.json`, authoritative simulator truth topics, rendered camera instance truth, and full radar XYZ/velocity/SNR/noise fields.\n\n- Bag: `CANARY/bag`\n- Source-tree hash: `{before['source_tree_sha256']}`\n- Bag hash: `{record['bag_sha256']}`\n- Tracking: `{json.dumps(rep['tracking']['metrics'],sort_keys=True)}`\n- World: Gazebo ENU; quaternion xyzw; linear/angular velocity expressed in world; source link `base_link`.\n- IDs: C0=conductor_4, C1=conductor_3, C2=conductor_2, C3=conductor_1.\n\nRun the strict contract and complete CANARY pipeline from the SLAM workspace exactly as specified by the handoff request. FINALTEST has not been recorded.\n''';(ROOT/'DATASET_HANDOFF.md').write_text(handoff);print(json.dumps({'status':'finalized','bag_sha256':record['bag_sha256'],'success':rep['success']},indent=2))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('command',choices=['prepare','finalize']);a=ap.parse_args();prepare() if a.command=='prepare' else finalize()
