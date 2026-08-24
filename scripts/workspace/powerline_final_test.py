#!/usr/bin/env python3
"""Prepare schema-v2 FINALTEST trajectory artifacts without scientific evaluation."""
import json,hashlib,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent));import powerline_qualification as q
import matplotlib;matplotlib.use('Agg');import matplotlib.pyplot as plt
ROOT=q.FINAL_TEST_OUTPUT
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
 g=q.Corridor.load();results=[]
 for spec in q.FINAL_FLIGHTS:
  tr=q.build_flight(spec,g);out,rep=q.write_flight(ROOT,tr,g);results.append((tr,out,rep));(out/'preflight_validation.json').write_text((out/'validation_report.json').read_text());d=json.loads((out/'trajectory_definition.json').read_text());d['powerline_qualification_schema_version']=2;(out/'trajectory_definition.json').write_text(json.dumps(d,indent=2,sort_keys=True)+'\n');m=json.loads((out/'flight_metadata.json').read_text());m['powerline_qualification_schema_version']=2;m['actual_applied_seeds']={'simulation':spec['seed'],'radar':spec['seed'],'camera':'Gazebo runtime simulation seed'};(out/'flight_metadata.json').write_text(json.dumps(m,indent=2,sort_keys=True)+'\n')
 canary=q.FINAL_CANARY_OUTPUT;freeze=json.loads((canary/'recorder_freeze.json').read_text());side=json.loads((canary/'corridor_frame.json').read_text());side['recorder_provenance']=freeze;ROOT.mkdir(parents=True,exist_ok=True);(ROOT/'corridor_frame.json').write_text(json.dumps(side,indent=2,sort_keys=True)+'\n');(ROOT/'recorder_freeze.json').write_text(json.dumps(freeze,indent=2,sort_keys=True)+'\n')
 manifest={'powerline_qualification_schema_version':2,'dataset':'powerline_qualification_final_test','dataset_role':'final_test','held_out':True,'flat_layout':True,'split_policy':{'unit':'whole flight','test_payload_open_count_before_lock':0},'truth_contract':{'schema_file':'powerline_qualification_schema_v2.json','schema_sha256':'5ea968c227dbf5df5aa58910d81e2afb67dd37031dc322db651effff26259753','corridor_sidecar':'corridor_frame.json','contract_only_validation_completed':False,'estimator_or_frontend_metrics_computed_before_lock':False},'recorder_build':{'workspace':'/home/iii/ws','source_tree_sha256':freeze['source_tree_sha256'],'recorder_command_sha256':freeze['recorder_command_sha256'],'truth_publisher_build_sha256':freeze['truth_publisher_build_sha256'],'message_interface_sha256':freeze['message_interface_sha256'],'calibration_sha256':freeze['calibration_sha256'],'trajectory_executor_sha256':freeze['trajectory_executor_sha256'],'trajectory_catalog_sha256':sha(q.ROOT/'scripts/workspace/powerline_qualification.py')},'flights':[{'flight_id':t.flight_id,'seed':t.seed,'role':t.name,'duration_s':float(t.t[-1]),'trajectory_type':t.name,'bag_path':f'{t.flight_id}/bag','trajectory_definition_sha256':sha(o/'trajectory_definition.json'),'preflight_status':r['status']} for t,o,r in results]};(ROOT/'dataset_manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
 fig,axs=plt.subplots(1,2,figsize=(14,6),dpi=160)
 for cid in g.ids:c=g.conductor_G(cid);axs[0].plot(c[:,0],c[:,1],'k',alpha=.3);axs[1].plot(c[:,0],c[:,2],'k',alpha=.3)
 for t,_,_ in results:axs[0].plot(t.p[:,0],t.p[:,1],label=t.flight_id);axs[1].plot(t.p[:,0],t.p[:,2],label=t.flight_id)
 for a in axs:a.grid();a.legend();a.axvline(-g.A,ls='--');a.axvline(g.A,ls='--')
 fig.tight_layout();fig.savefig(ROOT/'all_5_planned_truth_trajectories.png');plt.close(fig);print(json.dumps({'status':'passed' if all(r['status']=='passed' for _,_,r in results) else 'failed','checks':{t.flight_id:r['checks'] for t,_,r in results}},indent=2));return 0 if all(r['status']=='passed' for _,_,r in results) else 2
if __name__=='__main__':raise SystemExit(main())
